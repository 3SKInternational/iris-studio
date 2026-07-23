#!/bin/bash
# unison-bootstrap.sh — ONE-TIME safe enablement of Air<->Mini bidirectional sync.
# RUN ON THE AIR, with the Air awake and the Mini reachable, BEFORE installing
# the launchd job. Fail-closed at every gate. Nothing here propagates a deletion.
#
# This script encodes the #1 lesson of the 2026-05-27 Syncthing data-loss
# incident: NEVER enable bidirectional sync from a divergent state. It makes both
# sides byte-identical first (Mini canonical, rsync no --delete), THEN seeds the
# Unison archive from that identical state.
#
# It does NOT install the launchd job. Review the output, confirm a clean
# round-trip test, then install the job per the runbook.

set -uo pipefail

MINI_IP="100.118.108.65"
MINI_USER="steve"
MINI_ROOT="/Users/steve/Documents/3SK/outputs"
PROFILE="3sk-vault"
SENTINEL="CLAUDE.md"          # must exist in a real vault root
SSH_KEY="${HOME}/.ssh/id_ed25519_iris_mini_sync"
SSH_OPTS="-i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

# VERIFIED 2026-07-23: the Air's canonical vault is ~/Documents/3SK/outputs
# (9,580 files, has the sentinel). ~/3SK/outputs is the 163-file PARTIAL COPY
# left behind by the 5/27 Syncthing incident — it has NO sentinel, so the check
# below correctly rejects it. (CLAUDE.md's "Air is at ~/3SK/outputs" is wrong.)
# Sentinel, not order, is what decides — but list the real one first.
AIR_ROOT_CANDIDATES=("${HOME}/Documents/3SK/outputs" "${HOME}/3SK/outputs")

die() { echo "❌ BOOTSTRAP ABORTED: $*" >&2; exit 1; }
ok()  { echo "✅ $*"; }

echo "=== Unison Air<->Mini bootstrap (fail-closed) ==="

# --- Gate 1: locate unison on the Air ---------------------------------------
UNISON=""
for cand in "${HOME}/bin/unison" /opt/homebrew/bin/unison /usr/local/bin/unison "$(command -v unison 2>/dev/null)"; do
    [ -n "${cand}" ] && [ -x "${cand}" ] && { UNISON="${cand}"; break; }
done
[ -n "${UNISON}" ] || die "unison not installed on the Air. Run: brew install unison"
AIR_VER="$(${UNISON} -version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
ok "Air unison: ${UNISON} (v${AIR_VER:-unknown})"

# --- Gate 2: ssh key + Mini reachable ---------------------------------------
[ -f "${SSH_KEY}" ] || die "missing Air->Mini ssh key ${SSH_KEY} (generate + add pubkey to Mini authorized_keys)"
ssh -n ${SSH_OPTS} "${MINI_USER}@${MINI_IP}" true 2>/dev/null || die "Mini unreachable at ${MINI_USER}@${MINI_IP} (Tailscale up? Remote Login on? key authorized?)"
ok "Mini reachable over ssh"

# --- Gate 3: unison present + version-matched on the Mini -------------------
MINI_VER="$(ssh -n ${SSH_OPTS} "${MINI_USER}@${MINI_IP}" \
    'for c in $HOME/bin/unison /opt/homebrew/bin/unison /usr/local/bin/unison "$(command -v unison)"; do [ -x "$c" ] && { "$c" -version; break; }; done' 2>/dev/null \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ -n "${MINI_VER}" ] || die "unison not found on the Mini. ssh in and: brew install unison"
# Unison requires matching MAJOR.MINOR on both ends of a sync.
[ "${AIR_VER%.*}" = "${MINI_VER%.*}" ] || die "unison version mismatch — Air ${AIR_VER} vs Mini ${MINI_VER}. Match major.minor on both."
ok "Mini unison v${MINI_VER} — version-compatible"

# --- Gate 4: resolve + verify the Air vault root (sentinel) -----------------
AIR_ROOT=""
FOUND_COUNT=0
for c in "${AIR_ROOT_CANDIDATES[@]}"; do
    if [ -f "${c}/${SENTINEL}" ]; then
        FOUND_COUNT=$((FOUND_COUNT + 1))
        [ -z "${AIR_ROOT}" ] && AIR_ROOT="${c}"
    fi
done
[ -n "${AIR_ROOT}" ] || die "no Air vault found with ${SENTINEL} in: ${AIR_ROOT_CANDIDATES[*]}. Resolve the real Air vault path first."
# Refuse to guess if a stale duplicate vault exists in the other location.
[ "${FOUND_COUNT}" -eq 1 ] || die "TWO candidate vaults both contain ${SENTINEL}: ${AIR_ROOT_CANDIDATES[*]}. Remove/rename the stale one (likely a pre-5/27 copy in ~/Documents) so there is exactly one canonical Air vault, then re-run."
ok "Air vault root: ${AIR_ROOT}"

# --- Gate 5: verify the Mini vault root (sentinel) over ssh ------------------
ssh -n ${SSH_OPTS} "${MINI_USER}@${MINI_IP}" "test -f '${MINI_ROOT}/${SENTINEL}'" 2>/dev/null \
    || die "Mini vault sentinel ${MINI_ROOT}/${SENTINEL} not found — wrong MINI_ROOT?"
ok "Mini vault root: ${MINI_ROOT}"

# --- Gate 6: confirm the profile's roots match what we resolved -------------
echo
echo "About to make these two roots byte-identical (Mini canonical), then seed"
echo "the Unison archive from that identical state:"
echo "   AIR : ${AIR_ROOT}"
echo "   MINI: ${MINI_USER}@${MINI_IP}:${MINI_ROOT}"
echo
echo "⚠️  Ensure ~/.unison/${PROFILE}.prf root1 == '${AIR_ROOT}'."
read -r -p "Type EXACTLY 'identical' to proceed (anything else aborts): " CONFIRM
[ "${CONFIRM}" = "identical" ] || die "not confirmed."

# --- Step 7: make identical — rsync Mini -> Air, NO --delete ----------------
echo "Filling the Air from the Mini (rsync, no --delete; nothing on the Air is removed)..."
rsync -avhz --exclude '.DS_Store' --exclude '._*' -e "ssh ${SSH_OPTS}" \
    "${MINI_USER}@${MINI_IP}:${MINI_ROOT}/" "${AIR_ROOT}/" \
    || die "seed rsync failed — fix before enabling Unison."
ok "Air filled from Mini (no deletions applied)"

# --- Step 8: seed the Unison archive from the identical state ---------------
# -prefer the Mini root so the FIRST run treats Mini as authoritative on any
# residual diff. After this seed, normal runs use the profile (no -prefer).
echo "Seeding Unison archive (first run, Mini-preferred)..."
"${UNISON}" "${PROFILE}" -batch -prefer "ssh://${MINI_USER}@${MINI_IP}/${MINI_ROOT}" \
    || die "first unison run failed — DO NOT install the launchd job until this is clean."
ok "Unison archive seeded from an identical state"

# --- Step 9: arm the wrapper's shrink tripwire from the seeded state ---------
# MUST use the same prune set as count_files() in unison-sync-wrapper.sh so the
# baseline matches what the guard will measure.
COUNT_STATE="${HOME}/.unison/${PROFILE}.filecount"
SEED_COUNT="$(find "${AIR_ROOT}" -type f \
    -not -path '*/.git/*' -not -path '*/.trash/*' \
    -not -name '.DS_Store' -not -name '._*' -not -name '*.tmp' \
    2>/dev/null | wc -l | tr -d ' ')"
printf '%s\n' "${SEED_COUNT}" > "${COUNT_STATE}"
ok "Shrink tripwire armed from run 1: ${COUNT_STATE} = ${SEED_COUNT} files"

echo
echo "=== BOOTSTRAP COMPLETE ==="
echo "Next (do NOT skip): run the round-trip test in the runbook — create/edit/delete"
echo "a probe file on EACH machine and confirm it propagates — THEN install"
echo "com.iris.unison-sync.plist on the Air."
