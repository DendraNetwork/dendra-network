package keeper

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

func (k msgServer) CreateMiner(ctx context.Context, msg *types.MsgCreateMiner) (*types.MsgCreateMinerResponse, error) {
	// The signer's bytes are decoded once, here, and reused by the identity guard below and by the
	// bond further down. Decoding twice would let the guard and the escrow disagree about which
	// address is being registered if the two calls ever diverged.
	signerBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid address: %s", err))
	}

	// Identity guard (ADR-039): the miner id is derived from the signer, never chosen by it.
	//
	// `miner_id` cannot be free text supplied by the party it identifies. Three problems would follow,
	// all closed here:
	//   - Impersonation: nothing would stop a newcomer from registering `m-pc-01` or any name an operator
	//     is already publicly known by, and the registry key is what payouts, the payee journal and
	//     every dashboard read.
	//   - Leakage: the default id would be whatever the client has at hand, often a hostname, recorded
	//     on-chain and public forever.
	//   - Re-entry under a new name: an evicted or slashed identity could come back under a fresh
	//     string from the same key, and nothing on-chain would tie the two together.
	// A derived id makes the address-to-identity binding total and publicly recomputable: anyone can
	// check offline that a given miner id belongs to a given account, with no chain query.
	//
	// This guard deliberately sits before the bond: every check that can refuse the message must fire
	// before `SendCoinsFromAccountToModule` below. The state machine reverts on error, so the order
	// does not affect correctness today; still, a guard downstream of a fund movement is one refactor
	// away from a fund-loss bug, and keeping the order costs nothing.
	//
	// The refusal quotes the expected identifier. An error that only says "wrong id" forces the
	// operator to reimplement the derivation to find out what to send; the answer is already computed
	// here, so it is handed over.
	wantID, derr := types.DeriveMinerID(signerBz)
	if derr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "cannot derive the miner id: "+derr.Error())
	}
	if msg.MinerId != wantID {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			fmt.Sprintf("miner_id must be derived from the signer address, not chosen: expected %q, got %q. One address registers exactly one miner, under exactly one identifier.", wantID, msg.MinerId))
	}

	// Check if the value already exists
	ok, err := k.Miner.Has(ctx, msg.MinerId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	} else if ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "index already set")
	}

	// Minimum bond (ADR-018): refuse a stake below min_stake.
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Stake < params.MinStake {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			fmt.Sprintf("stake %d < min_stake %d", msg.Stake, params.MinStake))
	}

	// Real bond: the stake is escrowed as actual coins, from the signer into the jobs module account.
	// A `Stake` field that is only a counter lets a miner declare an enormous stake without depositing
	// anything, and a slash then removes nothing bankable. The bond is a real deposit held by the
	// module: a slash removes it for good, and exit refunds what is left. This fails when the signer
	// lacks the funds, so there is no miner without an effective bond.
	// `signerBz` is the same decode the identity guard used at the top of this function, so the address
	// that pays the bond is by construction the address the miner id was derived from.
	bond := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Stake)))
	if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(signerBz), types.ModuleName, bond); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "bond: "+err.Error())
	}

	// On-chain anchoring of the miner's X25519 encryption key, signed by its account key. The client
	// encrypts the prompt to this pubkey, read on-chain, rather than to the one announced to the relay,
	// which defeats pubkey substitution at the relay (MITM). Optional for backward compatibility, but
	// when present it must be a valid X25519 pubkey: 32 bytes, hex-encoded.
	if msg.EncPubkey != "" {
		if b, derr := hex.DecodeString(msg.EncPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid enc_pubkey (expected 32 X25519 bytes, hex-encoded)")
		}
	}
	// Ed25519 VRF pubkey anchored on-chain (ECVRF): used to prove availability with a verifiable VRF
	// proof, and later for committee selection. Optional; when present, 32 bytes hex.
	if msg.VrfPubkey != "" {
		if b, derr := hex.DecodeString(msg.VrfPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid vrf_pubkey (expected 32 Ed25519 bytes, hex-encoded)")
		}
	}
	// An empty Operator would make key rotation impossible forever, since RotateMinerKeys checks
	// ownership against it.
	if msg.Operator == "" {
		msg.Operator = msg.Creator
	}
	var miner = types.Miner{
		Creator:   msg.Creator,
		MinerId:   msg.MinerId,
		Operator:  msg.Operator,
		Region:    msg.Region,
		Stake:     msg.Stake,
		EncPubkey: msg.EncPubkey,
		VrfPubkey: msg.VrfPubkey,
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Date the registration (ADR-034). Without this date, "never committed" cannot be told apart from
	// "just arrived": the benefit of the doubt then covers registered and sterile identities, the
	// population the vitality guard targets, while the guard only catches miners that produced and
	// then stopped, the honest ones.
	//
	// There is deliberately no `h > 0` guard here (ADR-037 §3.b). `MinerRegisteredHeight` is compared
	// with `JobPoolFreezeHeight`, which `OpenJob` writes unconditionally. Two anchors that are compared
	// but written under different conditions diverge: at height 0 the job got its anchor and the
	// miner did not, so the miner was excluded from every draw (empty committee, "no primary
	// assigned", settlement impossible). Six end-to-end tests (`CreateMiner`, then `OpenJob`, then
	// `SettleSemantic`) failed on that. Two anchors that are compared must be written by the same
	// code, in the same block, with no asymmetric condition. `uint64(0)` is a valid height
	// ("registered at block 0"), not an absence, and absence keeps its own meaning because no path
	// produces it.
	if err := k.MinerRegisteredHeight.Set(ctx, miner.MinerId, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgCreateMinerResponse{}, nil
}

func (k msgServer) UpdateMiner(ctx context.Context, msg *types.MsgUpdateMiner) (*types.MsgUpdateMinerResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	val, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "index not set")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != val.Creator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "incorrect owner")
	}
	// The bond does not change through update, and the handler says so.
	// Changing a bond means exit (delete, hence refund) then creation again (new escrow): there is no
	// unbonding path, and this handler moves no coins. Discarding `msg.Stake` silently answered an
	// operator asking to reduce its bond with a success and an unchanged escrow. Refusing costs one
	// comparison and tells them where the real path is.
	// The comparison is against an explicit value, not against the field being present. proto3
	// omits a zero, so a client updating only `region` sends `stake` unset, which cannot be told apart
	// from `stake = 0`. Refusing on `msg.Stake != val.Stake` would reject every partial update, the
	// normal case. Zero here means "not asking", and asking for zero is `delete`.
	if msg.Stake != 0 && msg.Stake != val.Stake {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"update-miner cannot change the bond: the escrow is real, and this handler moves no coins. "+
				"To change it, delete the miner (the bond is refunded) and create it again with the new stake. "+
				"Leave `stake` unset (or equal to the current one) to update the other fields.")
	}
	var miner = types.Miner{
		Creator:  msg.Creator,
		MinerId:  msg.MinerId,
		Operator: msg.Operator,
		Region:   msg.Region,
		Stake:    val.Stake,
		// The anchored X25519 pubkey is kept on update; rotation goes through RotateMinerKeys.
		EncPubkey: val.EncPubkey,
		// The VRF pubkey is kept on update as well.
		VrfPubkey: val.VrfPubkey,
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to update miner")
	}
	return &types.MsgUpdateMinerResponse{}, nil
}

func (k msgServer) DeleteMiner(ctx context.Context, msg *types.MsgDeleteMiner) (*types.MsgDeleteMinerResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	val, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "index not set")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != val.Creator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "incorrect owner")
	}
	// Voluntary exit goes through the same guard as eviction. Without it, DeleteMiner is a back
	// door out of the slash: a disputed primary leaves before the verdict with 100 % of its stake
	// (the evasion hasOpenObligation closes at prune time, reopened through a permissionless path),
	// and a summoned juror can desert its seat. Enough deserters and the seat quorum becomes
	// unreachable forever: the retention and the dispute bond stay escrowed indefinitely, and the
	// griefing is free because exit refunds everything.
	// Fail closed on a store error, like prune: in doubt, nothing is released.
	// The way out is to honour the obligation (vote the verdict, let the audit resolve).
	if obligated, oErr := k.hasOpenObligation(ctx, msg.MinerId); oErr != nil || obligated {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"exit refused: open obligations (retained fee owed, unresolved audit-committee seat, or a job under dispute) — exit becomes possible again once the instance is closed")
	}
	// Refund the real remaining bond (after any slash) to the signer before removing the miner.
	// Whatever was already slashed is not refunded: it stayed in the module, in the treasury.
	if val.Stake > 0 {
		toBz, err := k.addressCodec.StringToBytes(val.Creator)
		if err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, err.Error())
		}
		refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(val.Stake)))
		if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), refund); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "refund bond: "+err.Error())
		}
	}
	if err := k.Miner.Remove(ctx, msg.MinerId); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to remove miner")
	}
	// A voluntary exit has to forget what eviction already forgets.
	// `pruneInactiveMiners` removes `MinerLastCommitHeight` when it evicts (miner_vitality.go), while
	// this path removed only the miner record. The id is derived from the signer address (ADR-039),
	// so a miner that leaves and comes back gets the same id and inherits its own stale last-commit
	// height. `eligibleAsJurorInWindow` reads that entry first and, when present, decides alone, so
	// the returning miner is judged on a commit it made before leaving and is drawn for nothing: not
	// work (committee.go), not the jury (audit_committee.go), and it does not count towards the
	// OpenJob floor (pool_freeze.go). Measured on the shipped msg servers: after DeleteMiner then
	// CreateMiner, reg=400000 while lastCommit=100, and the real work draw returns the control miner
	// alone. Eviction eventually clears the entry, but the dead band is the whole pruning window,
	// and a returning operator has no way to see why nothing comes.
	// The error is deliberately ignored: the bond refund above has already been sent, and failing here
	// would undo a payment over a bookkeeping entry.
	_ = k.MinerLastCommitHeight.Remove(ctx, msg.MinerId)
	// The other half of what registration wrote: `CreateMiner` writes two per-miner dates, and
	// leaving one behind makes the next reader guess which of them a departed id still carries.
	// It changes no decision today (both readers of `MinerRegisteredHeight` reach it through a live
	// miner, `Miner.Walk` or an id taken from the miner record, and the map is not exported at
	// genesis), so this is residue rather than a wrong answer. Residue in a store that no export
	// prunes is still an entry someone will read one day as a fact about a miner that is gone.
	// Error ignored for the same reason as above: the bond refund is already sent.
	_ = k.MinerRegisteredHeight.Remove(ctx, msg.MinerId)
	_ = k.MinerLastPresentEpoch.Remove(ctx, msg.MinerId) // ADR-048 item 5: a re-registered id starts as a newcomer
	return &types.MsgDeleteMinerResponse{}, nil
}

// RotateMinerKeys lets the operator replace the anchored pubkeys (X25519 encryption, Ed25519 VRF)
// without a Delete+Create, which would reset Demand to 0 and break the subsidy gate. An empty field
// leaves that key unchanged.
func (k msgServer) RotateMinerKeys(ctx context.Context, msg *types.MsgRotateMinerKeys) (*types.MsgRotateMinerKeysResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	val, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown miner")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != val.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "only the miner's operator may rotate its keys")
	}
	if msg.NewEncPubkey != "" {
		if b, derr := hex.DecodeString(msg.NewEncPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid new_enc_pubkey (32 bytes, hex-encoded)")
		}
		val.EncPubkey = msg.NewEncPubkey
	}
	if msg.NewVrfPubkey != "" {
		if b, derr := hex.DecodeString(msg.NewVrfPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid new_vrf_pubkey (32 bytes, hex-encoded)")
		}
		val.VrfPubkey = msg.NewVrfPubkey
	}
	// Demand, Stake, Operator and Region are kept: only the pubkeys move.
	if err := k.Miner.Set(ctx, msg.MinerId, val); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to rotate miner keys")
	}
	return &types.MsgRotateMinerKeysResponse{}, nil
}
