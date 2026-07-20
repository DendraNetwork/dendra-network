package keeper

import "cosmossdk.io/collections"

// commitRange borne un parcours de `Commit` aux seules clés portant un préfixe donné.
//
// LE PROBLÈME. Les clés de `Commit` ont la forme "<jobId>__<minerId>" (et ses variantes
// "<jobId>__redo__<minerId>", "<jobId>__verdict__<minerId>"). Huit handlers — règlement, paiement,
// finalisation, adjudication, vérification sémantique, anti-évasion — parcouraient la collection
// ENTIÈRE via `Walk(ctx, nil, …)` puis jetaient en mémoire tout ce qui ne concernait pas leur job.
// Le coût d'un règlement croissait donc avec l'historique COMPLET de la chaîne, et non avec la
// taille du job réglé.
//
// POURQUOI C'EST GRAVE PLUS TARD, ET INVISIBLE AUJOURD'HUI. À quelques milliers de commits le
// surcoût ne se voit pas. Mais il est MONOTONE : il ne peut que croître, et il croît pour des
// transactions dont le contenu, lui, ne change pas. Passé un seuil, le gas d'un règlement dépasse
// la limite de bloc et plus AUCUN job ne se règle — un gel total du protocole, déclenché par le
// simple passage du temps, sans qu'aucune transaction fautive ne soit jamais soumise. C'est le
// genre de dette qu'on ne peut plus corriger au moment où elle se manifeste : la migration
// elle-même exigerait de faire passer des transactions.
//
// CORRECTION. `Prefix` restreint le parcours au sous-ensemble des clés concernées, en s'appuyant
// sur l'ordre lexicographique des clés encodées — que `StringKey` préserve pour une clé terminale.
// Aucune collision entre jobs n'est possible : "j1__" ne préfixe pas les clés de "j10", qui
// commencent par "j10" ("0" ≠ "_"). Le coût redevient proportionnel au nombre de commits DU job.
//
// Les appelants gardent leur `strings.HasPrefix` : il ne sert plus à sélectionner (le range s'en
// charge) mais de défense en profondeur. Il est délibérément conservé — s'il disparaissait et que
// le bornage se révélait plus large que prévu, des commits d'un AUTRE job entreraient dans un
// décompte de paiement. Le filtre est gratuit sur des clés déjà chargées ; c'est le bornage, pas
// lui, qui est mesuré par le test (lequel compte les clés effectivement visitées).
func commitRange(prefix string) *collections.Range[string] {
	return new(collections.Range[string]).Prefix(prefix)
}
