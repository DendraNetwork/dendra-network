package app

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"os"
	"strings"

	abci "github.com/cometbft/cometbft/abci/types"
	"github.com/cosmos/cosmos-sdk/baseapp"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/mempool"

	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519"

	"dendra/x/jobs/vrf"
)

// ============================================================================
// E4 — VRF DÉCENTRALISÉE par vote-extensions ABCI++ (briques 3/4).
//
// Chaque validateur contribue, à chaque hauteur, une SORTIE VRF vérifiable (via sa clé VRF ancrée
// on-chain par MsgRegisterValidatorVrfKey). Le proposant agrège ces sorties en UNE graine imprévisible
// et infalsifiable (cf. AggregateBeacons) — aucun acteur unique ne la contrôle. Cette brique 3 câble la
// PRODUCTION (ExtendVote) et la VÉRIFICATION (VerifyVoteExtension) ; l'agrégation/injection (brique 4)
// suit dans un lot séparé, testé sur le devnet 2-validateurs.
//
// DORMANT par défaut : CometBFT n'appelle ces handlers QUE si consensus_params.abci.
// vote_extensions_enable_height est posé (>0) et atteint. Tant qu'il ne l'est pas, ce câblage est
// strictement inerte -> la chaîne actuelle est intacte.
// ============================================================================

// vrfVoteDomain sépare le domaine de l'alpha des vote-extensions (anti collision avec d'autres usages).
var vrfVoteDomain = []byte("dendra/vrf/ve/v1")

// vrfVoteAlphaBase : partie déterministe et publique de l'alpha = domain ‖ height(8 octets big-endian).
func vrfVoteAlphaBase(height int64) []byte {
	var h [8]byte
	binary.BigEndian.PutUint64(h[:], uint64(height))
	return append(append([]byte{}, vrfVoteDomain...), h[:]...)
}

// vrfVoteAlphaWithHash (VE-01 durci, audit v7) — alpha = domain‖height‖HASH-DU-BLOC. Le hash du bloc en
// cours de vote est imprévisible AVANT que ce bloc soit proposé -> anti-précalcul fort (~0 bloc d'avance,
// vs ~1 pour le chaînage de graine). Le hash vient de la requête ABCI (RequestExtendVote.Hash /
// RequestVerifyVoteExtension.Hash), lié au BlockID du précommit -> IDENTIQUE côté producteur et vérificateur
// pour un même vote (pas de risque de liveness). Repli (hash absent, ex. bootstrap) sur le CHAÎNAGE de graine
// (DecentralizedSeed[height-1]) puis sur domain‖height -> dégrade proprement, jamais bloquant.
func (app *App) vrfVoteAlphaWithHash(ctx sdk.Context, height int64, hash []byte) []byte {
	a := vrfVoteAlphaBase(height)
	if len(hash) > 0 {
		return append(a, hash...)
	}
	if height > 1 {
		if prev, ok := app.JobsKeeper.GetDecentralizedSeed(ctx, height-1); ok && len(prev) > 0 {
			return append(a, prev...)
		}
	}
	return a
}

// vrfVoteAlphaForAggregation (VE-01) — alpha DÉTERMINISTE pour RE-vérifier, à l'agrégation (PreBlocker), les
// extensions produites à extHeight. On relit le HASH du bloc extHeight STOCKÉ par le PreBlocker à cette hauteur
// (= hash du bloc finalisé), pour matcher EXACTEMENT ce que ExtendVote/VerifyVoteExtension ont signé. Hash
// absent (1er bloc actif) -> même repli que côté production -> agrégation cohérente sur tous les nœuds.
func (app *App) vrfVoteAlphaForAggregation(ctx sdk.Context, extHeight int64) []byte {
	if h, ok := app.JobsKeeper.GetBlockHash(ctx, extHeight); ok && len(h) > 0 {
		return app.vrfVoteAlphaWithHash(ctx, extHeight, h)
	}
	return app.vrfVoteAlphaWithHash(ctx, extHeight, nil)
}

// loadNodeVrfKey charge la clé PRIVÉE VRF du nœud depuis le fichier désigné par DENDRA_VRF_KEY_FILE
// (hex d'une clé Ed25519 de 64 octets, p.ex. produite par `dendra-vrf keygen`). Absente/illisible/
// invalide -> nil : le nœud ne contribue alors PAS de preuve VRF (sa vote-extension est vide, ce qui
// reste valide et n'altère pas la liveness).
func loadNodeVrfKey() ed25519.PrivateKey {
	path := strings.TrimSpace(os.Getenv("DENDRA_VRF_KEY_FILE"))
	if path == "" {
		return nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	b, err := hex.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil || len(b) != ed25519.PrivateKeySize {
		return nil
	}
	return ed25519.PrivateKey(b)
}

// setupVoteExtensions câble les handlers ABCI++ ExtendVote / VerifyVoteExtension. Appelé dans New()
// AVANT app.Load() (donc avant le scellement du baseapp). Inerte tant que les vote-extensions sont OFF.
func (app *App) setupVoteExtensions() {
	nodeSK := loadNodeVrfKey() // peut être nil (nœud sans clé VRF -> extension vide)

	// ExtendVote : le nœud signe alpha(height) avec SA clé VRF -> preuve ECVRF (80 octets) en extension.
	// Ne JAMAIS faire échouer le vote pour un souci VRF (renvoie une extension vide à la place).
	app.SetExtendVoteHandler(func(ctx sdk.Context, req *abci.RequestExtendVote) (*abci.ResponseExtendVote, error) {
		if len(nodeSK) != ed25519.PrivateKeySize {
			return &abci.ResponseExtendVote{VoteExtension: []byte{}}, nil
		}
		pi, err := vrf.Prove(nodeSK, app.vrfVoteAlphaWithHash(ctx, req.Height, req.Hash))
		if err != nil {
			return &abci.ResponseExtendVote{VoteExtension: []byte{}}, nil
		}
		return &abci.ResponseExtendVote{VoteExtension: pi}, nil
	})

	// VerifyVoteExtension : LENIENT (éviter tout risque de liveness). Vide -> ACCEPT (pas de contribution) ;
	// mauvaise taille -> REJECT ; sinon, si la clé VRF du validateur est ancrée on-chain on vérifie la
	// preuve et on REJECT si elle est forgée. Clé non ancrée -> ACCEPT (l'agrégation ne retiendra de toute
	// façon que les preuves vérifiées contre une clé ancrée).
	app.SetVerifyVoteExtensionHandler(func(ctx sdk.Context, req *abci.RequestVerifyVoteExtension) (*abci.ResponseVerifyVoteExtension, error) {
		ext := req.VoteExtension
		if len(ext) == 0 {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_ACCEPT}, nil
		}
		if len(ext) != vrf.ProofSize {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_REJECT}, nil
		}
		pk := app.validatorVrfPubkey(ctx, req.ValidatorAddress)
		if pk == nil {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_ACCEPT}, nil
		}
		if ok, _ := vrf.Verify(pk, app.vrfVoteAlphaWithHash(ctx, req.Height, req.Hash), ext); !ok {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_REJECT}, nil
		}
		return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_ACCEPT}, nil
	})

	// ---- BRIQUE 4 : agrégation décentralisée de la graine (PrepareProposal / ProcessProposal / PreBlocker) ----
	// Handlers PAR DÉFAUT auxquels déléguer quand les vote-extensions sont INACTIVES : ainsi, dormant =
	// comportement STRICTEMENT inchangé (sélection mempool + re-vérification des tx proposées d'origine).
	defaultPH := baseapp.NewDefaultProposalHandler(mempool.NoOpMempool{}, app)
	defaultPrepare := defaultPH.PrepareProposalHandler()
	defaultProcess := defaultPH.ProcessProposalHandler()

	// PrepareProposal : le proposant agrège-able -> INJECTE le commit étendu (les vote-extensions de H-1)
	// en tête du bloc, après l'avoir validé (signatures + 2/3 de puissance via ValidateVoteExtensions).
	app.SetPrepareProposal(func(ctx sdk.Context, req *abci.RequestPrepareProposal) (*abci.ResponsePrepareProposal, error) {
		if !voteExtensionsActive(ctx, req.Height) {
			return defaultPrepare(ctx, req)
		}
		if err := baseapp.ValidateVoteExtensions(ctx, app.StakingKeeper, req.Height, ctx.ChainID(), req.LocalLastCommit); err != nil {
			// VE-03 (audit v7) : sur un VRAI réseau (signatures strictes) un échec ici ferait retomber
			// SILENCIEUSEMENT sur le défaut -> VRF décentralisée inactive sans qu'on le voie. On LOG l'échec
			// (observabilité = le vrai correctif). NB SDK 0.53 : height/chainID sont ignorés par la fonction
			// (relus du ctx) ; on les passe quand même pour la justesse + forward-compat (retirés en v0.51+).
			ctx.Logger().Error("E4 ValidateVoteExtensions echec -> repli (graine VRF decentralisee INACTIVE ce bloc)", "height", req.Height, "err", err)
			return defaultPrepare(ctx, req)
		}
		bz, err := req.LocalLastCommit.Marshal()
		if err != nil {
			return defaultPrepare(ctx, req)
		}
		inj := append(append([]byte{}, vrfInjectPrefix...), bz...)
		return &abci.ResponsePrepareProposal{Txs: prependWithinBudget(inj, req.Txs, req.MaxTxBytes)}, nil
	})

	// ProcessProposal : si une injection est présente, la VALIDER (commit étendu bien formé + signatures) ;
	// sinon, comportement par défaut. (REJECT bloque la proposition -> sécurité.)
	app.SetProcessProposal(func(ctx sdk.Context, req *abci.RequestProcessProposal) (*abci.ResponseProcessProposal, error) {
		if voteExtensionsActive(ctx, req.Height) && len(req.Txs) > 0 && bytes.HasPrefix(req.Txs[0], vrfInjectPrefix) {
			var ec abci.ExtendedCommitInfo
			if err := ec.Unmarshal(req.Txs[0][len(vrfInjectPrefix):]); err != nil {
				return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_REJECT}, nil
			}
			if err := baseapp.ValidateVoteExtensions(ctx, app.StakingKeeper, req.Height, ctx.ChainID(), ec); err != nil {
				ctx.Logger().Error("E4 ProcessProposal: ValidateVoteExtensions a rejete l'injection", "height", req.Height, "err", err)
				return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_REJECT}, nil
			}
			return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_ACCEPT}, nil
		}
		return defaultProcess(ctx, req)
	})

	// PreBlocker : consomme l'injection -> re-agrège la graine (déterministe) + la STOCKE on-chain, PUIS
	// exécute le PreBlock des modules (préserve la logique upgrade etc.). Inactif/sans injection -> seulement PreBlock.
	app.SetPreBlocker(func(ctx sdk.Context, req *abci.RequestFinalizeBlock) (*sdk.ResponsePreBlock, error) {
		if voteExtensionsActive(ctx, req.Height) {
			// VE-01 : on mémorise le hash de CE bloc -> l'agrégation du bloc SUIVANT relira ce hash pour
			// re-vérifier déterministe les extensions de cette hauteur (alpha lié au bloc). Dormant => aucun write.
			if err := app.JobsKeeper.SetBlockHash(ctx, req.Height, req.Hash); err != nil {
				ctx.Logger().Error("E4 set block hash", "err", err)
			}
			// V8-N1 : purge fenêtrée (anti-bloat KV) — supprime BlockHash + DecentralizedSeed (+ contributeurs
			// et puissance, lot scaling 2026-07-02) loin derrière. L'alpha n'utilise que h et h-1 ;
			// vrfStatePruneWindow=1000 = marge très large. No-op avant H=1000.
			if old := req.Height - vrfStatePruneWindow; old > 0 {
				_ = app.JobsKeeper.DeleteBlockHash(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeed(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeedContributorPower(ctx, old)
			}
			if len(req.Txs) > 0 && bytes.HasPrefix(req.Txs[0], vrfInjectPrefix) {
				var ec abci.ExtendedCommitInfo
				if err := ec.Unmarshal(req.Txs[0][len(vrfInjectPrefix):]); err == nil {
					if seed, n, powerBps, ok := app.aggregateSeed(ctx, ec, req.Height-1); ok {
						if err := app.JobsKeeper.SetDecentralizedSeed(ctx, req.Height, seed); err != nil {
							ctx.Logger().Error("E4 set decentralized seed", "err", err)
						} else {
							ctx.Logger().Info("E4 decentralized VRF seed", "height", req.Height, "contributors", n, "contributor_power_bps", powerBps, "seed", hex.EncodeToString(seed))
							// Bootstrap VRF (internal audit 2026-06-26) : stocke le NB de contributeurs -> lu par committeeBaseSeed
							// pour le plancher committee_min_vrf_contributors (anti-régression silencieuse).
							if err := app.JobsKeeper.SetDecentralizedSeedContributors(ctx, req.Height, uint64(n)); err != nil {
								ctx.Logger().Error("E4 set decentralized seed contributors", "err", err)
							}
							// LOT SCALING (2026-07-02, post-red-team) : stocke la part de PUISSANCE des contributeurs
							// (bps du pouvoir total du commit) -> plancher dynamique ⌈2N/3⌉ EN POUVOIR de
							// committeeBaseSeed (anti sybil-poussière). Même site d'écriture que la graine.
							if err := app.JobsKeeper.SetDecentralizedSeedContributorPower(ctx, req.Height, powerBps); err != nil {
								ctx.Logger().Error("E4 set decentralized seed contributor power", "err", err)
							}
						}
					}
				}
			}
		}
		return app.ModuleManager.PreBlock(ctx)
	})
}

// vrfInjectPrefix marque l'unique "tx" d'injection (en tête de bloc) portant le commit étendu marshalé.
var vrfInjectPrefix = []byte("DENDRA_VE1")

// vrfStatePruneWindow (V8-N1) — nb de blocs de BlockHash/DecentralizedSeed conservés (fenêtre glissante
// anti-bloat). L'alpha VRF ne lit que h et h-1 ; 1000 = marge très large (observabilité/snapshot) qui borne
// néanmoins le store sur un réseau durable. Le PreBlocker supprime l'entrée à req.Height-vrfStatePruneWindow.
const vrfStatePruneWindow = 1000

// voteExtensionsActive : les vote-extensions sont-elles consommables à cette hauteur ? Les extensions de
// H-1 n'existent qu'une fois vote_extensions_enable_height atteint -> injection à H requiert H > enableHeight.
func voteExtensionsActive(ctx sdk.Context, height int64) bool {
	cp := ctx.ConsensusParams()
	if cp.Abci == nil || cp.Abci.VoteExtensionsEnableHeight <= 0 {
		return false
	}
	return height > cp.Abci.VoteExtensionsEnableHeight
}

// prependWithinBudget place l'injection en tête puis ajoute les tx du mempool tant que MaxTxBytes le permet.
func prependWithinBudget(inj []byte, txs [][]byte, maxBytes int64) [][]byte {
	out := [][]byte{inj}
	budget := maxBytes - int64(len(inj))
	for _, tx := range txs {
		budget -= int64(len(tx))
		if budget < 0 {
			break
		}
		out = append(out, tx)
	}
	return out
}

// aggregateSeed re-calcule la graine décentralisée à partir d'un commit étendu : pour chaque vote, résout
// la clé VRF ancrée du validateur, VÉRIFIE sa preuve contre alpha(extHeight), et agrège les beta valides.
// Déterministe (AggregateBeacons trie ; entrée = commit injecté, identique sur tous les nœuds).
// Renvoie (seed, nbContributeurs, partPuissanceBps, ok) ; ok=false si aucun beta valide.
// LOT SCALING (2026-07-02, post-red-team) : partPuissanceBps = Σ puissance des votes à preuve VRF VALIDE ÷
// Σ puissance de TOUS les votes du commit (×10000). Les validateurs absents/sans preuve comptent au dénominateur
// (conservateur). C'est la mesure anti sybil-poussière du plancher ⌈2N/3⌉ (un cardinal serait griefable).
func (app *App) aggregateSeed(ctx sdk.Context, ec abci.ExtendedCommitInfo, extHeight int64) ([]byte, int, uint64, bool) {
	alpha := app.vrfVoteAlphaForAggregation(ctx, extHeight)
	var betas [][]byte
	var contribPower, totalPower int64
	for _, v := range ec.Votes {
		totalPower += v.Validator.Power
		if len(v.VoteExtension) != vrf.ProofSize {
			continue
		}
		pk := app.validatorVrfPubkey(ctx, v.Validator.Address)
		if pk == nil {
			continue
		}
		if ok, beta := vrf.Verify(pk, alpha, v.VoteExtension); ok {
			betas = append(betas, beta)
			contribPower += v.Validator.Power
		}
	}
	if len(betas) == 0 {
		return nil, 0, 0, false
	}
	seed, err := vrf.AggregateBeacons(betas)
	if err != nil {
		return nil, 0, 0, false
	}
	var powerBps uint64
	if totalPower > 0 && contribPower >= 0 {
		powerBps = uint64(contribPower) * 10000 / uint64(totalPower)
	}
	return seed, len(betas), powerBps, true
}

// validatorVrfPubkey résout la clé PUBLIQUE VRF ancrée d'un validateur à partir de son adresse de
// CONSENSUS (fournie par ABCI) : consAddr -> validateur (staking) -> compte d'opérateur -> clé ancrée.
// Renvoie nil si introuvable / non ancrée / invalide.
func (app *App) validatorVrfPubkey(ctx sdk.Context, consAddr []byte) ed25519.PublicKey {
	validator, err := app.StakingKeeper.GetValidatorByConsAddr(ctx, sdk.ConsAddress(consAddr))
	if err != nil {
		return nil
	}
	valBz, err := sdk.ValAddressFromBech32(validator.OperatorAddress)
	if err != nil {
		return nil
	}
	// La clé VRF du validateur est ancrée sous son compte d'opérateur (mêmes octets que le valoper).
	accStr := sdk.AccAddress(valBz).String()
	hexpk, err := app.JobsKeeper.ValidatorVrfPubkey.Get(ctx, accStr)
	if err != nil {
		return nil
	}
	b, err := hex.DecodeString(hexpk)
	if err != nil || len(b) != ed25519.PublicKeySize {
		return nil
	}
	return ed25519.PublicKey(b)
}
