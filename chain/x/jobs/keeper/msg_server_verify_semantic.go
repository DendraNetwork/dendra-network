package keeper

import (
	"context"
	"errors"
	"math/big"
	"sort"
	"strconv"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// Seuil semantique GOUVERNE (GO-03). En dur en attendant un champ Params dedie
// (la regen proto exige ignite/protoc ; cf. _audit-fixes/README). Valeur bps (7000 = cos >= 0.70).
const semanticThresholdBps int64 = 7000

// Bornes d'entree (GO-15) : dimension max + magnitude par composante.
const (
	embedMaxDim = 384 // DOC-13 : assez pour un vrai embedder semantique (ex. all-MiniLM-L6-v2 = 384 dim)
	embedMaxMag = 1 << 20
)

// VerifySemantic -- S2 mode SEMANTIQUE (ADR-020) : clustering par cosinus entier des embeddings ancres.
//
// CORRIGE (audit 2026-06-10) :
//
//	GO-01 : anti-rejeu -- charge le Job, refuse si "+verified", ecrit le flag (verifie) en fin.
//	        Sans ca, n'importe qui rejouait VerifySemantic pour reduire a 0 le stake d'un honnete.
//	GO-03 : seuil = constante GOUVERNEE (semanticThresholdBps), plus msg.ThresholdBps (attaquant).
//	GO-05 : cosinus en math/big (cosineGE) -> plus d'overflow int64.
//	GO-06 : comite COMPLET requis (>= CommitteeSize) + majorite STRICTE ; si pas de majorite stricte,
//	        on ne slashe PERSONNE (evite le slash arbitraire).
//	GO-15 : parseIntVec borne (dimension + magnitude), cosineGE exige des longueurs egales.
func (k msgServer) VerifySemantic(ctx context.Context, msg *types.MsgVerifySemantic) (*types.MsgVerifySemanticResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// GO-01 : anti-rejeu.
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job non ouvert")
	}
	if strings.Contains(job.State, "verified") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja verifie (anti-rejeu)")
	}
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	prefix := msg.JobId + "__"
	vecs := map[string][]int64{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				if v := parseIntVec(c.ResultCommit); v != nil {
					vecs[mid] = v
				}
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// GO-06 : comite COMPLET (sinon 2 mineurs se valident mutuellement avec required=1).
	if len(vecs) < CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "comite incomplet -> verification refusee")
	}

	mids := make([]string, 0, len(vecs))
	for mid := range vecs {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	required := len(vecs)/2 + 1 // GO-06 : majorite STRICTE (> n/2)

	neighbors := map[string]int{}
	majorityExists := false
	for _, mid := range mids {
		c := 0
		for _, other := range mids {
			if other != mid && cosineGE(vecs[mid], vecs[other], semanticThresholdBps) {
				c++
			}
		}
		neighbors[mid] = c
		if c+1 >= required {
			majorityExists = true
		}
	}
	if !majorityExists {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pas de majorite semantique stricte -> aucun slash")
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	for _, mid := range mids {
		if neighbors[mid]+1 >= required {
			continue // dans la majorite stricte
		}
		miner, mErr := k.Miner.Get(ctx, mid) // OUTLIER -> slash
		if mErr != nil {
			continue
		}
		amt := miner.Stake * params.SlashLeakBps / 10000
		miner.Stake -= amt
		if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		pools.Treasury += amt
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// GO-01 : marque verifie (erreur verifiee).
	job.State = job.State + "+verified"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "echec marquage +verified : "+err.Error())
	}
	return &types.MsgVerifySemanticResponse{}, nil
}

// parseIntVec -- parse "n0,n1,..." en []int64. GO-15 : borne dimension + magnitude.
// DOC-13 : composantes SIGNÉES (les vrais embeddings sentence-transformers ont des valeurs < 0),
// bornées en magnitude |v| <= embedMaxMag. Le sac-de-mots actuel (non-négatif) reste un cas valide.
// (Utilise aussi par settle_semantic.go -> garder le nom.)
func parseIntVec(s string) []int64 {
	parts := strings.Split(s, ",")
	if len(parts) == 0 || len(parts) > embedMaxDim {
		return nil
	}
	out := make([]int64, 0, len(parts))
	for _, p := range parts {
		v, err := strconv.ParseInt(strings.TrimSpace(p), 10, 64)
		if err != nil || v < -embedMaxMag || v > embedMaxMag {
			return nil
		}
		out = append(out, v)
	}
	return out
}

// cosineGE : cos(a,b) >= tbps/10000 en grands entiers (math/big) -> AUCUN overflow (GO-05).
// GO-15 : exige des vecteurs de MEME longueur (sinon cosinus mal defini).
// DOC-13 : composantes signées OK -> le garde `dot.Sign() < 0 -> false` empeche qu'un cosinus
// NEGATIF passe a tort le seuil (le test se fait sur dot², qui perdrait le signe).
// (Utilise aussi par settle_semantic.go -> garder le nom + la signature.)
func cosineGE(a, b []int64, tbps int64) bool {
	if len(a) != len(b) || len(a) == 0 {
		return false
	}
	dot, na, nb := new(big.Int), new(big.Int), new(big.Int)
	t := new(big.Int)
	for i := range a {
		dot.Add(dot, t.Mul(big.NewInt(a[i]), big.NewInt(b[i])))
	}
	for i := range a {
		na.Add(na, t.Mul(big.NewInt(a[i]), big.NewInt(a[i])))
	}
	for i := range b {
		nb.Add(nb, t.Mul(big.NewInt(b[i]), big.NewInt(b[i])))
	}
	if dot.Sign() < 0 || na.Sign() == 0 || nb.Sign() == 0 {
		return false
	}
	lhs := new(big.Int).Mul(dot, dot)
	lhs.Mul(lhs, big.NewInt(100000000))
	rhs := new(big.Int).Mul(big.NewInt(tbps), big.NewInt(tbps))
	rhs.Mul(rhs, na)
	rhs.Mul(rhs, nb)
	return lhs.Cmp(rhs) >= 0
}
