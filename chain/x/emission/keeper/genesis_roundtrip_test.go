package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/emission/types"
)

// LE MOTEUR D'ÉMISSION DOIT SURVIVRE À UN EXPORT/IMPORT.
//
// `InitGenesis` remettait la Réserve à sa valeur de genèse et l'époque à 0 À CHAQUE appel, sans
// regarder le genesis. Un export/import — le geste normal d'une migration — avait donc trois effets,
// tous silencieux : une Réserve déjà dépensée revenait PLEINE, donc le même calendrier de libération
// était joué deux fois ; les pools non réclamés étaient effacés, donc les subventions dues aux mineurs
// disparaissaient pendant que les coins qui les adossaient étaient re-comptés comme Réserve ; et
// `last_epoch = 0` face à une hauteur déjà élevée déclenchait une époque au premier bloc.
//
// Le test est écrit pour ÉCHOUER sur l'ancien code : la Réserve exportée (1000) est volontairement
// très éloignée de `GenesisReserveU`, et les pools sont non nuls. Un round-trip qui ne transporterait
// rien rendrait la valeur de genèse, pas 1000.
func TestEmissionGenesisRoundTripPreservesTheEngine(t *testing.T) {
	f := initFixture(t)

	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, 1000))
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 77))
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 33))
	require.NoError(t, f.keeper.SecurityPool.Set(f.ctx, 11))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 4200))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 9_999_999))

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.NotNil(t, exported.State, "l'etat du moteur doit etre exporte, pas seulement les params")

	g := initFixture(t)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported))

	res, err := g.keeper.Reserve.Get(g.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), res,
		"une Reserve deja depensee ne doit PAS revenir pleine : ce serait liberer deux fois le meme calendrier")

	wp, _ := g.keeper.WorkPool.Get(g.ctx)
	ap, _ := g.keeper.AvailPool.Get(g.ctx)
	sp, _ := g.keeper.SecurityPool.Get(g.ctx)
	require.Equal(t, uint64(77), wp, "subventions travail non reclamees : effacees = dues a personne")
	require.Equal(t, uint64(33), ap)
	require.Equal(t, uint64(11), sp)

	le, _ := g.keeper.LastEpoch.Get(g.ctx)
	require.Equal(t, uint64(4200), le,
		"last_epoch=0 face a une hauteur elevee ferait tomber une epoque des le premier bloc")
	ls, _ := g.keeper.LastSupply.Get(g.ctx)
	require.Equal(t, uint64(9_999_999), ls)
}

// UNE CHAÎNE NEUVE S'AMORCE ENCORE. La garde ne doit pas empêcher le cas nominal : sans bloc `state`
// (genesis écrit à la main, `config.yml`, binaire antérieur), le moteur doit démarrer sur la Réserve
// de genèse — sinon on aurait remplacé une résurrection de fonds par une chaîne qui n'émet jamais.
func TestEmissionGenesisWithoutStateStillBootstraps(t *testing.T) {
	f := initFixture(t)
	legacy := types.DefaultGenesis() // State == nil, comme un export d'avant ce lot
	require.Nil(t, legacy.State)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *legacy))

	res, err := f.keeper.Reserve.Get(f.ctx)
	require.NoError(t, err)
	require.NotZero(t, res, "chaine neuve : le moteur doit etre amorce sur la Reserve de genese")
}

// UNE ÉPOQUE EN AVANCE SUR LA CHAÎNE NE DOIT PAS RENDRE L'ÉMISSION MUETTE.
//
// `last_epoch` est une hauteur ABSOLUE. Restaurée sur une chaîne qui redémarre plus bas (un import
// à hauteur 1, ce que `forZeroHeight` produit), la condition « pas encore l'heure » reste vraie
// pendant toute la distance à rattraper — et l'émission se tait sans erreur ni journal. Le module
// `x/jobs` a résolu le cas jumeau en transportant du temps restant ; ici la donnée est un point de
// départ, pas une échéance, donc le remède est de refuser le silence : on recale et on le dit.
func TestEmissionFutureLastEpochDoesNotSilenceEmission(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.EpochBlocks = 300
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, 1_000_000))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 9000)) // état issu d'une chaîne bien plus haute

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5) // la chaîne, elle, repart de presque zéro
	require.NoError(t, f.keeper.RunEpoch(ctx))

	le, err := f.keeper.LastEpoch.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(5), le,
		"last_epoch doit etre recale sur la hauteur courante, sinon l'emission attend 8995 blocs EN SILENCE")
}

// ET UNE RÉSERVE LÉGITIMEMENT ÉPUISÉE RESTE À ZÉRO. C'est le cas que proto3 seul ne sait pas
// distinguer de « non renseigné », et la raison pour laquelle `state` est un message optionnel :
// un bloc PRÉSENT dont la Réserve vaut 0 signifie « il ne reste rien », pas « recommence ».
func TestEmissionExhaustedReserveIsNotResurrected(t *testing.T) {
	f := initFixture(t)
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: 0, LastEpoch: 12345}
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *g))

	res, err := f.keeper.Reserve.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), res, "une Reserve epuisee vaut zero et doit le rester")
	le, _ := f.keeper.LastEpoch.Get(f.ctx)
	require.Equal(t, uint64(12345), le)
}
