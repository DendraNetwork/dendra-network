package types

import (
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/msgservice"
)

func RegisterInterfaces(registrar codectypes.InterfaceRegistry) {
	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgSettleSemantic{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgProveAvailability{},
		&MsgSubmitVrfBeacon{},
		&MsgRotateMinerKeys{},
		&MsgRegisterValidatorVrfKey{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgCreateBeacon{},
		&MsgUpdateBeacon{},
		&MsgDeleteBeacon{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgVerifySemantic{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgPayout{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgOpenJob{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgSettlePay{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgFinalizeJob{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgCreateCommit{},
		&MsgUpdateCommit{},
		&MsgDeleteCommit{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgPayReal{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgRewardTraining{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgReportDivergence{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgDisputeVerdict{},
		&MsgResolveDispute{},
		&MsgAdjudicateDispute{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgClaimSubsidy{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgSlashMiner{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgSettleJob{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgCreatePools{},
		&MsgUpdatePools{},
		&MsgDeletePools{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgCreateJob{},
		&MsgUpdateJob{},
		&MsgDeleteJob{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgCreateMiner{},
		&MsgUpdateMiner{},
		&MsgDeleteMiner{},
	)

	registrar.RegisterImplementations((*sdk.Msg)(nil),
		&MsgUpdateParams{},
	)
	msgservice.RegisterMsgServiceDesc(registrar, &_Msg_serviceDesc)
}
