package app

import (
	"context"
	"fmt"

	storetypes "cosmossdk.io/store/types"
	upgradetypes "cosmossdk.io/x/upgrade/types"
	"github.com/cosmos/cosmos-sdk/types/module"
)

// MONTÉES DE VERSION COORDONNÉES PAR LA CHAÎNE ELLE-MÊME.
//
// Ce que ce fichier ferme. `UpgradeKeeper` était câblé (module enregistré, `SetModuleVersionMap`
// appelé au genesis) mais AUCUN handler nommé n'était enregistré. Conséquences, dans cet ordre de
// gravité :
//
//  1. Une proposition `MsgSoftwareUpgrade` votée HALTE la chaîne à la hauteur prévue et refuse de
//     repartir : « unknown upgrade <nom> ». La voie de montée officielle de Cosmos était donc une
//     panne garantie, et personne ne l'apprenait avant de l'avoir déclenchée en production.
//  2. Faute de cette voie, chaque changement consensus-breaking se faisait à la main : arrêter tous
//     les validateurs, remplacer le binaire, redémarrer. Avec 2 validateurs à 50/50, arrêter l'un
//     arrête la production de blocs — ce qui a au moins le mérite de rendre impossible une fenêtre
//     mixte. À 4 validateurs et plus, ce filet disparaît : deux nœuds sur l'ancien code et deux sur
//     le nouveau, c'est un FORK, pas une indisponibilité.
//
// CE QUE CE FICHIER N'APPORTE PAS, et il faut être net là-dessus. Un handler d'upgrade coordonne le
// MOMENT du changement (tout le monde s'arrête à la même hauteur, et un nœud resté sur l'ancien
// binaire refuse de continuer au lieu de forker en silence). Il ne rend PAS un changement de LOGIQUE
// rejouable depuis le genesis : l'historique contient réellement deux comportements, et aucune
// migration ne réécrit des blocs déjà signés. Pour ça, le remède reste snapshots + state-sync, déjà
// livrés. Confondre les deux mènerait à croire l'historique réparé alors qu'il ne l'est pas.

// UpgradeName — nom du plan à voter. Il DOIT correspondre exactement au champ `name` de la
// proposition, sinon la chaîne halte sur « unknown upgrade ».
const UpgradeName = "v2-anchored-committees"

// RegisterUpgradeHandlers enregistre le handler du plan courant + le chargeur de stores.
// Appelé depuis New() une fois les keepers construits.
func (app *App) RegisterUpgradeHandlers() {
	app.UpgradeKeeper.SetUpgradeHandler(
		UpgradeName,
		func(ctx context.Context, _ upgradetypes.Plan, fromVM module.VersionMap) (module.VersionMap, error) {
			// RunMigrations exécute les migrations des modules dont la ConsensusVersion a bougé.
			// Un changement de logique à ConsensusVersion CONSTANTE (le cas d'ADR-032/033) n'a rien à
			// migrer : ici le handler ne sert qu'à SYNCHRONISER la bascule, ce qui est précisément ce
			// qu'on veut. Ne pas confondre « aucune migration » avec « aucun effet ».
			return app.ModuleManager.RunMigrations(ctx, app.Configurator(), fromVM)
		},
	)

	// CHARGEUR DE STORES. Nécessaire uniquement quand une montée AJOUTE ou SUPPRIME un module : sans
	// lui, le store du module neuf n'existe pas au premier bloc post-upgrade et le nœud panique.
	// `ReadUpgradeInfoFromDisk` lit le plan que l'UpgradeKeeper a écrit avant de halter.
	upgradeInfo, err := app.UpgradeKeeper.ReadUpgradeInfoFromDisk()
	if err != nil {
		panic(fmt.Errorf("lecture du plan d'upgrade sur disque: %w", err))
	}
	if upgradeInfo.Name == UpgradeName && !app.UpgradeKeeper.IsSkipHeight(upgradeInfo.Height) {
		// Aucun store ajouté ni supprimé par ce plan. On enregistre quand même le chargeur : il est
		// alors un no-op explicite, et la prochaine montée n'a qu'à remplir `Added`/`Deleted` au lieu
		// de redécouvrir qu'il manquait. Une liste vide se relit ; un chargeur absent ne se voit pas.
		app.SetStoreLoader(upgradetypes.UpgradeStoreLoader(
			upgradeInfo.Height,
			&storetypes.StoreUpgrades{},
		))
	}
}
