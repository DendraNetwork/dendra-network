package keeper

import (
	"context"
	"strings"

	"dendra/x/modelregistry/types"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
// Pose les Params PUIS les modèles autorisés (épinglage au genesis, sans gouvernance).
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	if err := k.Params.Set(ctx, genState.Params); err != nil {
		return err
	}
	// ÉCART DE GARDE ENTRE LES DEUX CHEMINS D'ÉCRITURE, rendu VISIBLE.
	//
	// `RegisterModel` (msg_server_model.go) REFUSE un modèle sans `weights_sha256` : l'empreinte est ce
	// qui lie un `model_id` déclaré dans un commit à un artefact réel. Le genesis, lui, n'exigeait rien.
	// Un modèle posé ACTIF avec une empreinte VIDE traverse donc `ExpectedWeights` en « pas d'ancre »,
	// et `x/jobs` n'exige alors AUCUNE correspondance : le mineur annonce le modèle et sert ce qu'il veut.
	//
	// On ne REFUSE pas ici, et c'est délibéré : le genesis de développement pose précisément des
	// modèles sans empreinte (placeholder devnet assumé). Refuser ferait paniquer le démarrage d'une
	// chaîne neuve — on remplacerait une garde manquante par une chaîne qui ne démarre pas. Ce qui
	// était réellement fautif, c'est le SILENCE : rien ne disait que la liaison était désarmée.
	//
	// ⚠️ PRÉCONDITION D'OUVERTURE INCITÉE : cette alerte ne doit plus apparaître. Tant qu'elle apparaît,
	// la propriété « le juge est contraint par le modèle » n'est pas tenue on-chain pour ces modèles.
	var unanchored []string
	for _, m := range genState.Models {
		if err := k.Models.Set(ctx, m.Id, m); err != nil {
			return err
		}
		if m.Active && strings.TrimSpace(m.WeightsSha256) == "" {
			unanchored = append(unanchored, m.Id)
		}
	}
	if len(unanchored) > 0 {
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"SECURITE: modele(s) ACTIF(S) au genesis SANS weights_sha256 -> aucune liaison model_id<->artefact : un mineur peut declarer ce modele et servir autre chose. RegisterModel refuse ce cas ; le genesis ne le refusait pas. A corriger AVANT toute ouverture incitee.",
			"modeles", strings.Join(unanchored, ","), "nb", len(unanchored))
	}
	return nil
}

// ExportGenesis returns the module's exported genesis.
func (k Keeper) ExportGenesis(ctx context.Context) (*types.GenesisState, error) {
	var err error

	genesis := types.DefaultGenesis()
	genesis.Params, err = k.Params.Get(ctx)
	if err != nil {
		return nil, err
	}

	// exporter les modèles enregistrés (round-trip genesis).
	if err = k.Models.Walk(ctx, nil, func(_ string, m types.Model) (bool, error) {
		genesis.Models = append(genesis.Models, m)
		return false, nil
	}); err != nil {
		return nil, err
	}

	return genesis, nil
}
