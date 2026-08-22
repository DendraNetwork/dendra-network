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
	// The signer's bytes are decoded ONCE, here, and reused by the identity guard below and by the
	// bond further down. Decoding twice would let the guard and the escrow disagree about which
	// address is being registered if the two calls ever diverged.
	signerBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid address: %s", err))
	}

	// IDENTITY GUARD (ADR-039) — the miner id is DERIVED from the signer, never chosen by it.
	//
	// `miner_id` used to be free text supplied by the party it identifies. Three things follow from
	// that, and all three are closed here:
	//   - IMPERSONATION. Nothing stopped a newcomer from registering `m-pc-01` or any name an
	//     operator was already publicly known by; the registry key is what payouts, the payee journal
	//     and every dashboard read.
	//   - LEAKAGE. The default id was whatever the client had to hand — a hostname — engraved on-chain
	//     and public forever.
	//   - RE-ENTRY UNDER A NEW NAME. An evicted or slashed identity could come back under a fresh
	//     string from the same key, and nothing on-chain tied the two together.
	// A derived id makes the address ↔ identity binding total and publicly recomputable: anyone can
	// check offline that a given miner id belongs to a given account, with no chain query.
	//
	// THIS GUARD IS PLACED BEFORE THE BOND ON PURPOSE. Every check that can refuse the message must
	// fire before `SendCoinsFromAccountToModule` below. Refusing after the escrow leaves the caller's
	// coins moved by a transaction that errored — the state machine reverts on error today, so the
	// order is not what makes it correct, but a guard that sits downstream of a fund movement is one
	// refactor away from being a fund-loss bug, and the ordering costs nothing to keep.
	//
	// The refusal QUOTES the expected identifier. An error that only says "wrong id" forces the
	// operator to reimplement the derivation to find out what to send; the answer is already computed
	// here, so it is handed over.
	wantID, derr := types.DeriveMinerID(signerBz)
	if derr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "cannot derive the miner id: "+derr.Error())
	}
	if msg.MinerId != wantID {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			fmt.Sprintf("miner_id must be DERIVED from the signer address, not chosen: expected %q, got %q. One address registers exactly one miner, under exactly one identifier.", wantID, msg.MinerId))
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

	// REAL BOND. The stake is escrowed as ACTUAL coins, from the signer into the jobs module account.
	// A `Stake` field that is only a COUNTER lets a miner declare an enormous stake without depositing
	// anything, and a slash then removes nothing bankable — skin in the game that is not there. The
	// bond is a real deposit held by the module: a slash removes it for good, and exit refunds what is
	// left. This fails when the signer lacks the funds, so there is no miner without an effective bond.
	// `signerBz` is the SAME decode the identity guard used at the top of this function: the address
	// that pays the bond is by construction the address the miner id was derived from.
	bond := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Stake)))
	if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(signerBz), types.ModuleName, bond); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "bond: "+err.Error())
	}

	// ON-CHAIN ANCHORING of the miner's X25519 encryption key, signed by its Cosmos key. The client
	// encrypts the prompt to THIS pubkey, read on-chain, rather than the one announced to the relay,
	// which makes pubkey substitution at the relay (MITM) inoperative. Optional for backward
	// compatibility, but when present it MUST be a valid X25519 pubkey: 32 bytes, hex-encoded.
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
	// DATE THE REGISTRATION (ADR-034). Without this date, "never committed" is indistinguishable from
	// "just arrived": the benefit of the doubt then covers registered-and-sterile identities — exactly
	// the population the vitality guard targets — while the guard bites only on miners that produced
	// and then stopped, i.e. the honest ones.
	//
	// There is deliberately NO `h > 0` guard here (ADR-037 §3.b). `MinerRegisteredHeight` is COMPARED
	// against `JobPoolFreezeHeight`, which `OpenJob` writes UNCONDITIONALLY. Two anchors that are
	// compared but written under DIFFERENT conditions diverge: at height 0 the job got its anchor and
	// the miner did not, so the miner was excluded from EVERY draw — empty committee, "no primary
	// assigned", settlement impossible. Six end-to-end tests (`CreateMiner`, then `OpenJob`, then
	// `SettleSemantic`) failed on exactly that. The rule is general: two anchors that are compared must
	// be written by the same code, in the same block, with NO asymmetric condition. `uint64(0)` is a
	// valid height — "registered at block 0" — not an absence, and absence keeps its own distinct
	// meaning because no path produces it.
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
	// THE BOND DOES NOT CHANGE THROUGH UPDATE, AND SAYING SO IS PART OF THE RULE.
	// Changing a bond means exit (delete, hence refund) then re-creation (new escrow): there is no
	// unbonding path, and this handler moves no coins. Discarding `msg.Stake` in silence answered an
	// operator asking to reduce its bond with a SUCCESS and an unchanged escrow — a field the proto
	// offers, that a caller can set, and whose effect is nothing. Refusing costs one comparison and
	// tells them where the real path is.
	// ⛔ AND THE COMPARISON IS AGAINST AN *EXPLICIT* VALUE, NOT AGAINST THE FIELD BEING PRESENT.
	// proto3 omits a zero, so a client updating only `region` sends `stake` unset — indistinguishable
	// from `stake = 0`. Refusing on `msg.Stake != val.Stake` would therefore reject every partial
	// update, which is the normal case. Zero here means "not asking", and asking for zero is `delete`.
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
		// The anchored X25519 pubkey is PRESERVED on update; rotation goes through RotateMinerKeys.
		EncPubkey: val.EncPubkey,
		// The VRF pubkey is PRESERVED on update as well.
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
	// VOLUNTARY EXIT GOES THROUGH THE SAME GUARD AS EVICTION. Without it, DeleteMiner is the service
	// entrance to the slash: a disputed primary leaves BEFORE the verdict with 100 % of its stake — the
	// evasion that hasOpenObligation closes at prune time, reopened through a permissionless path — and
	// a summoned juror can desert its seat. Enough deserters and the seat quorum becomes unreachable
	// FOREVER: the retention and the dispute bond stay escrowed indefinitely, and the griefing is free
	// because exit refunds everything.
	// Fail closed on a store error, exactly like prune: in doubt, nothing is released.
	// The way out is to honour the obligation — vote the verdict, let the audit resolve — never to flee.
	if obligated, oErr := k.hasOpenObligation(ctx, msg.MinerId); oErr != nil || obligated {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"exit refused: open obligations (retained fee owed, unresolved audit-committee seat, or a job under dispute) — exit becomes possible again once the instance is closed")
	}
	// Refund the REAL remaining bond (post-slash) to the signer before removing the miner. Whatever was
	// already slashed is NOT refunded: it stayed in the module, in the treasury. Exit is a clean exit.
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
	// LEAVING VOLUNTARILY HAD TO FORGET WHAT EVICTION ALREADY FORGETS.
	// `pruneInactiveMiners` removes `MinerLastCommitHeight` when it evicts (miner_vitality.go); this
	// path removed only the miner record. The id is DERIVED from the signer address (ADR-039), so a
	// miner that leaves and comes back gets the SAME id and inherits its own stale last-commit height.
	// `eligibleAsJurorInWindow` reads that entry FIRST and, when present, decides alone -- so the
	// returning miner is judged on a commit it made before leaving and is drawn for nothing: not work
	// (committee.go), not the jury (audit_committee.go), and it does not count toward the OpenJob floor
	// (pool_freeze.go). Measured on the shipped msg servers: after DeleteMiner then CreateMiner,
	// reg=400000 while lastCommit=100, and the real work draw returns the control miner alone.
	// It is not permanent -- eviction eventually clears the entry -- but the dead band is the whole
	// pruning window, and a returning operator has no way to see why nothing comes.
	// The error is ignored on purpose: the bond refund above has already been sent, and failing here
	// would undo a payment over a bookkeeping entry.
	_ = k.MinerLastCommitHeight.Remove(ctx, msg.MinerId)
	// AND THE OTHER HALF OF WHAT REGISTRATION WROTE. `CreateMiner` writes TWO per-miner dates;
	// leaving one behind makes the next reader guess which of them a departed id still carries.
	// It changes no decision today — both readers of `MinerRegisteredHeight` reach it through a
	// live miner (`Miner.Walk`, or an id taken from the miner record), and the map is not exported
	// at genesis — so this is residue, not a wrong answer. Residue in a store that no export prunes
	// is still an entry someone will one day read as a fact about a miner that no longer exists.
	// Error ignored for the same reason as above: the bond refund is already sent.
	_ = k.MinerRegisteredHeight.Remove(ctx, msg.MinerId)
	return &types.MsgDeleteMinerResponse{}, nil
}

// RotateMinerKeys lets the OPERATOR replace the anchored pubkeys (X25519 encryption, Ed25519 VRF)
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
	// Demand, Stake, Operator and Region are PRESERVED: only the pubkeys move.
	if err := k.Miner.Set(ctx, msg.MinerId, val); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to rotate miner keys")
	}
	return &types.MsgRotateMinerKeysResponse{}, nil
}
