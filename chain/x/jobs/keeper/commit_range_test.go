package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// PARCOURS BORNÉ AU JOB COURANT.
//
// Huit handlers (règlement, paiement, finalisation, adjudication, vérification sémantique,
// anti-évasion) parcouraient TOUTE la collection `Commit` puis filtraient en mémoire. Le coût d'un
// règlement croissait donc avec l'historique complet de la chaîne, pour des transactions dont le
// contenu ne changeait pas — jusqu'au jour où le gas dépasse la limite de bloc et où plus aucun job
// ne se règle.
//
// Ce test porte sur le COÛT, pas sur le résultat : les appelants gardent un `strings.HasPrefix`,
// donc un bornage défaillant produirait quand même la bonne réponse, en scannant tout. C'est
// pourquoi on compte les clés effectivement VISITÉES, et qu'on les compare à un parcours non borné.
func TestCommitWalkIsBoundedToOneJob(t *testing.T) {
	f := initFixture(t)

	// "j10" est le piège : il commence par "j1" mais ses clés ne commencent PAS par "j1__".
	seeded := []string{"j1__mA", "j1__mB", "j1__redo__mA", "j10__mA", "j2__mA", "j2__mB"}
	for _, key := range seeded {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, key, types.Commit{JobId: key}))
	}

	visited := []string{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("j1__"),
		func(key string, _ types.Commit) (bool, error) {
			visited = append(visited, key)
			return false, nil
		}))

	// Seules les clés du job j1 sont VISITÉES — les autres ne sont même pas lues depuis le store.
	require.ElementsMatch(t, []string{"j1__mA", "j1__mB", "j1__redo__mA"}, visited,
		"le parcours doit s'arreter aux cles du job courant")
	require.NotContains(t, visited, "j10__mA",
		"collision de prefixe : j10 ne doit pas etre confondu avec j1")

	// PREUVE PAR CONTRASTE — c'est ce que faisait le code d'avant. Si ce compteur devenait égal au
	// précédent, le bornage ne bornerait rien et le test ci-dessus passerait quand même.
	all := 0
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, nil, func(string, types.Commit) (bool, error) {
		all++
		return false, nil
	}))
	require.Equal(t, len(seeded), all)
	require.Less(t, len(visited), all,
		"sans bornage le parcours lit les commits des AUTRES jobs : c'est exactement le cout supprime")

	// Un préfixe plus long (variantes __redo__ / __verdict__) doit se borner de la même façon :
	// c'est la même primitive qui sert aux huit appelants, elle ne doit pas dépendre de la forme.
	redo := []string{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("j1__redo__"),
		func(key string, _ types.Commit) (bool, error) {
			redo = append(redo, key)
			return false, nil
		}))
	require.Equal(t, []string{"j1__redo__mA"}, redo)
}
