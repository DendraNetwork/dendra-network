package keeper

import (
	"context"

	"cosmossdk.io/collections"
)

// export_test.go — hooks de test (compilés UNIQUEMENT par `go test`, jamais dans le binaire).
//
// Les tests vivent dans le package externe `keeper_test` : ils ne voient donc pas les méthodes non
// exportées. Ce fichier expose le strict nécessaire, sans élargir la surface publique du module en
// production — c'est le motif standard Go pour tester une primitive interne.

// DrawAuditCommitteeForTest expose `drawAuditCommittee` (ADR-032).
// Testé directement parce que le DÉTERMINISME du tirage est une propriété de consensus : deux
// validateurs qui tireraient des listes différentes ancreraient des valeurs différentes et forkeraient
// la chaîne. Ça ne se vérifie pas « par le comportement », ça se vérifie sur la fonction elle-même.
func (k Keeper) DrawAuditCommitteeForTest(ctx context.Context, seed, jobId, primaryId, disputerId string) ([]string, error) {
	return k.drawAuditCommittee(ctx, seed, jobId, primaryId, disputerId)
}

// AuditDeferStrideForTest expose le pas de report d'audit.
//
// Un test qui écrirait la valeur en dur (« le job doit être à h+1 ») teste le RÉGLAGE, pas la
// propriété — et casse dès qu'on ajuste le pas pour une raison de coût, ce qui est arrivé. En le
// lisant, le test affirme ce qui compte : le job est REPORTÉ, à la hauteur que le code a choisie.
const AuditDeferStrideForTest = auditDeferStride

// CommitRangeForTest expose `commitRange`.
//
// Le bornage d'un parcours est une propriété de COÛT, pas de résultat. Les appelants conservent leur
// filtre `strings.HasPrefix` en défense en profondeur : un range défaillant rendrait donc quand même
// le BON résultat — en scannant simplement toute la collection, c'est-à-dire en reproduisant très
// exactement le défaut qu'on prétend avoir corrigé. Seul un test qui compte les clés VISITÉES sait
// distinguer « borné » de « filtré après coup », d'où l'exposition de la primitive elle-même.
func CommitRangeForTest(prefix string) *collections.Range[string] {
	return commitRange(prefix)
}
