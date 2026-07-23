#!/usr/bin/env python3
"""
manifest_spine_lint.py — deterministic PRE-SPEND lint for a 3SK Finance image manifest.

Makes the "invented / mismatched figure on a data card" failure class FREE instead of
costing a billed re-roll or a reviewer pass. Maps 1:1 to the documented RENDERS/PROMPTS
gate failure classes in
  BRANDS/3SK_Finance/Raw_Assets/Image_Factory/HOW_COWORK_USES_THIS.md
(§"Known RENDERS/PROMPTS-gate failure classes").

Read-only. stdlib only (runs under the daemon venv). Never writes a manifest or a render.

Usage:
  manifest_spine_lint.py <manifest.json> <script.md>
  manifest_spine_lint.py --video 09           # derive both by number
  manifest_spine_lint.py --video 09 --verbose  # also print the full report
  manifest_spine_lint.py --selftest            # run the review-gate fixtures
Flags: --quiet-ok (suppress the clean stdout line), --report-only (always exit 0).

Exit: 0 = clean or WARN-only · 1 = one or more FAILs (or a selftest failure) · 2 = usage error.

Checks (FAIL blocks a spend, WARN is advisory):
  1a FAIL  dollar-figure trace  — every $-figure on a card prompt must appear in the script
  1b WARN  percent-figure trace — every %-figure should appear (spelled-out "seventy percent"
                                    forms can't be cheaply verified, hence WARN not FAIL)
  2  WARN  TTS-spelling leak    — a spaced acronym ("P M I") inside a card prompt
  3  WARN  thumbnail title-zone — a Thumbnail entry lacking a reserved-zone phrase, or a
                                    frame-spanning verb with no pinning qualifier
  4  WARN  un-guarded text bait — a scene prop (ladder/laptop/screen/...) with no NO-TEXT guard
  5  WARN  speech-act guard     — multi-figure + interaction cue with no NO SPEECH BUBBLES
  6  FAIL  structural           — JSON parse error, duplicate names, or banned vocab
                                    (cinematic/realistic/...) used outside an explicit negation
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_DIR = os.path.join(REPO, "image_factory", "manifests")
VAULT = "/Users/steve/Documents/3SK/outputs"
FIN = os.path.join(VAULT, "BRANDS", "3SK_Finance")
SCRIPTS_DIR = os.path.join(FIN, "Scripts")
REPORT = os.path.join(FIN, "Raw_Assets", "_manifest_spine_report.md")
NOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.sh")

# --- figure extraction / normalization --------------------------------------
# A card figure passes only if its normalized CORE is an EXACT member of the set of
# cores extracted from the script — substring matching would false-pass $28,500 inside
# $285,000. "$38.4" (card) matches the script's "$38.4 million" because both reduce to
# the core "38.4"; the trailing scale word is script prose the $-regex stops before.
DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
PCT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
# Script prose spells percents as words ("5 percent", "6.8 percent") — capture the number
# so a card "%" figure still traces. Fully spelled-out numbers ("seventy percent") stay a
# WARN, which is why 1b is WARN and not FAIL.
PCT_WORD_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*percent\b", re.IGNORECASE)

# banned image vocab (class 6) — flagged only when NOT negated within the lookback window
# NOTE (open, 2026-07-22): canon bans "dramatic lighting", not bare "dramatic" —
# Master_Character_Prompt.md:174 and the sibling agent_output_lint.py BANNED_STRICT
# both say "dramatic lighting"; bare "dramatic" here dates to the original commit
# and looks like a truncation. Narrowing it would kill this false-positive class at
# the root but stops catching "dramatic shadows"/"dramatic contrast" — a coverage
# call for Steve, deliberately NOT bundled into the 7/22 negation fix. Live blast
# radius today is one false positive (Video_05_Shot_11a, already-published video).
BANNED = ("cinematic", "realistic", "soft lighting", "dramatic", "noir")
NEGATION_WINDOW = 30
NEGATION_RE = re.compile(r"\b(no|not|never|avoid|without|non)\b", re.IGNORECASE)

SPACED_ACRONYM_RE = re.compile(r"\b(?:[A-Z] ){2,}[A-Z]\b")  # "P M I" — case-sensitive
RESERVED_ZONE_RE = re.compile(
    r"ke(?:ep|pt)[^.]*?(?:clear|empty)|reserved title|title (?:zone|band)", re.IGNORECASE
)
FRAME_SPAN_RE = re.compile(r"grows across|spans|crosses", re.IGNORECASE)
PIN_QUALIFIER_RE = re.compile(r"only along|pinned|stays|except", re.IGNORECASE)
TEXT_BAIT_RE = re.compile(
    r"\b(ladder|laptop|screen|paper|sign|calendar|portrait|chart|whiteboard)\b", re.IGNORECASE
)
NO_TEXT_GUARD_RE = re.compile(r"no\s+text|text[- ]free|no\s+lettering|no\s+words", re.IGNORECASE)
MULTI_FIGURE_RE = re.compile(r"silhouette|figures|another person", re.IGNORECASE)
INTERACTION_RE = re.compile(r"facing|talking|speaking|conversation|hands over|gesturing", re.IGNORECASE)
NO_SPEECH_RE = re.compile(r"no speech bubbles", re.IGNORECASE)


def _core(tok):
    return re.sub(r"[,\s$%]", "", tok)


def _is_card(prompt):
    u = prompt.upper()
    return "NO CHARACTER" in u or "NO PEOPLE" in u


def script_figure_sets(script):
    dollars = {_core(x) for x in DOLLAR_RE.findall(script)}
    pcts = {_core(x) for x in PCT_RE.findall(script)}
    pcts |= {_core(x) for x in PCT_WORD_RE.findall(script)}
    return dollars, pcts


# A banned term is also negated when a negating PREFIX is fused onto it —
# "undramatic", "nondramatic", "unrealistic". NEGATION_RE only matches negation
# as a standalone word, so it caught "non-dramatic" (hyphen = word boundary) but
# not "undramatic", which FAILed V13's Shot_23a on the phrase "entirely
# undramatic" — the exact opposite of what the banned-vocab rule is protecting.
# The house style is flat/calm/undramatic, so this false positive recurs by
# design. Checked against the text immediately preceding the match, not the
# 30-char window, so it cannot swallow a genuinely un-negated later use.
# \Z not $ — Python's $ also matches immediately BEFORE a trailing newline, so a
# lookback ending in "a\n" read the article as a fused prefix and silently passed
# "on a\ndramatic ladder". 14 of 1,608 prompts across the manifests contain literal
# newlines, so that false negative was reachable on every banned term.
# Only un-/non-: those are the sole real negating prefixes for these five terms
# (undramatic, unrealistic, uncinematic, non-noir). "a" and "in" bought zero true
# positives and were pure false-negative surface.
PREFIX_NEGATION_RE = re.compile(r"(?:\b|[^a-z])(?:un|non)-?\Z", re.IGNORECASE)


def _negated(prompt, start):
    if NEGATION_RE.search(prompt[max(0, start - NEGATION_WINDOW):start]):
        return True
    return bool(PREFIX_NEGATION_RE.search(prompt[max(0, start - 5):start]))


# --- the lint ---------------------------------------------------------------
def lint(manifest, script):
    """Return a list of (level, check, name, message). level in {FAIL, WARN}."""
    findings = []
    images = manifest.get("images", [])

    # 6 structural — duplicate names
    seen = set()
    for im in images:
        nm = im.get("name", "")
        if nm in seen:
            findings.append(("FAIL", "structural", nm, "duplicate manifest name"))
        seen.add(nm)

    sd, sp = script_figure_sets(script)

    for im in images:
        nm = im.get("name", "")
        prompt = im.get("prompt", "")
        card = _is_card(prompt)

        # 1a/1b figure trace
        for d in DOLLAR_RE.findall(prompt):
            if _core(d) not in sd:
                findings.append(("FAIL", "spine-figure", nm,
                                 "card figure %s is not in the script number-spine" % d.strip()))
        for p in PCT_RE.findall(prompt):
            if _core(p) not in sp:
                findings.append(("WARN", "spine-figure", nm,
                                 "percent figure %s not found in the script (verify spelled-out form)" % p.strip()))

        # 6 structural — banned vocab outside a negation
        for term in BANNED:
            for mt in re.finditer(re.escape(term), prompt, re.IGNORECASE):
                if not _negated(prompt, mt.start()):
                    findings.append(("FAIL", "banned-vocab", nm,
                                     "banned image vocab '%s' used without a negation" % term))
                    break  # one per term is enough signal

        # 2 TTS spaced-acronym leak (card text)
        if card:
            for mt in SPACED_ACRONYM_RE.findall(prompt):
                findings.append(("WARN", "tts-spacing", nm,
                                 "spaced-acronym '%s' in card text (renders as letters, not the word)" % mt))

        # 3 thumbnail reserved title zone
        if "thumbnail" in nm.lower():
            if not RESERVED_ZONE_RE.search(prompt):
                findings.append(("WARN", "thumb-title-zone", nm,
                                 "thumbnail prompt has no reserved title-zone phrase"))
            if FRAME_SPAN_RE.search(prompt) and not PIN_QUALIFIER_RE.search(prompt):
                findings.append(("WARN", "thumb-title-zone", nm,
                                 "frame-spanning element with no pinning qualifier (may cross the title zone)"))

        # 4 un-guarded text-bait prop (scene shots only — cards want text)
        if not card:
            mt = TEXT_BAIT_RE.search(prompt)
            if mt and not NO_TEXT_GUARD_RE.search(prompt):
                findings.append(("WARN", "text-bait", nm,
                                 "prop '%s' with no NO-TEXT guard (garbled-text risk)" % mt.group(1)))

            # 5 speech-act guard
            if MULTI_FIGURE_RE.search(prompt) and INTERACTION_RE.search(prompt) \
                    and not NO_SPEECH_RE.search(prompt):
                findings.append(("WARN", "speech-act", nm,
                                 "multi-figure interaction with no NO SPEECH BUBBLES guard"))

    return findings


def load_manifest(path):
    """Return (manifest_dict, structural_findings). On parse error: (None, [FAIL])."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), []
    except json.JSONDecodeError as e:
        return None, [("FAIL", "structural", os.path.basename(path), "JSON parse error: %s" % e)]


# --- report / notify --------------------------------------------------------
def write_report(manifest_path, script_path, findings):
    fails = [f for f in findings if f[0] == "FAIL"]
    warns = [f for f in findings if f[0] == "WARN"]
    verdict = "FAIL" if fails else ("WARN" if warns else "CLEAN")
    lines = [
        "# Manifest spine lint — %s" % verdict,
        "",
        "- Manifest: `%s`" % manifest_path,
        "- Script: `%s`" % script_path,
        "- %d FAIL · %d WARN" % (len(fails), len(warns)),
        "",
    ]
    if not findings:
        lines.append("Clean — no spine-figure, structural, or QC findings.")
    for level in ("FAIL", "WARN"):
        rows = [f for f in findings if f[0] == level]
        if rows:
            lines.append("## %s" % level)
            for _, check, nm, msg in rows:
                lines.append("- **%s** [%s] %s" % (nm, check, msg))
            lines.append("")
    try:
        os.makedirs(os.path.dirname(REPORT), exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass  # report is a convenience; the exit code + stdout are the contract
    return verdict


def notify(msg):
    if not os.access(NOTIFY, os.X_OK):
        return
    try:
        subprocess.run([NOTIFY, msg], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass  # best-effort; never block the check on a notify failure


def derive_paths(video):
    n = "%02d" % int(video)
    manifest = os.path.join(MANIFEST_DIR, "Video_%s_orchestrated.json" % n)
    script = os.path.join(SCRIPTS_DIR, "Video_%s_Script.md" % n)
    return manifest, script


def run(manifest_path, script_path):
    if not os.path.exists(manifest_path):
        print("manifest_spine_lint: manifest not found: %s" % manifest_path, file=sys.stderr)
        return 2, "MISSING", []
    if not os.path.exists(script_path):
        print("manifest_spine_lint: script not found: %s" % script_path, file=sys.stderr)
        return 2, "MISSING", []
    manifest, findings = load_manifest(manifest_path)
    if manifest is None:  # JSON parse error → structural FAIL
        verdict = write_report(manifest_path, script_path, findings)
        return 1, verdict, findings
    with open(script_path, encoding="utf-8") as fh:
        script = fh.read()
    findings += lint(manifest, script)
    verdict = write_report(manifest_path, script_path, findings)
    rc = 1 if any(f[0] == "FAIL" for f in findings) else 0
    return rc, verdict, findings


def summary_line(verdict, findings):
    n_fail = sum(1 for f in findings if f[0] == "FAIL")
    n_warn = sum(1 for f in findings if f[0] == "WARN")
    if verdict == "FAIL":
        first = next(f for f in findings if f[0] == "FAIL")
        return "manifest_spine_lint: 🔴 FAIL — %s: %s (%d fail, %d warn)" % (first[2], first[3], n_fail, n_warn)
    return "manifest_spine_lint: %s (%d fail, %d warn)" % (verdict, n_fail, n_warn)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic pre-spend image-manifest lint (read-only).")
    ap.add_argument("manifest", nargs="?", help="path to the manifest JSON")
    ap.add_argument("script", nargs="?", help="path to the video script markdown")
    ap.add_argument("--video", help="derive manifest + script by video number (e.g. 09)")
    ap.add_argument("--verbose", action="store_true", help="print the full report to stdout")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    ap.add_argument("--quiet-ok", action="store_true", help="suppress the clean-run stdout line")
    ap.add_argument("--selftest", action="store_true", help="run the review-gate fixtures")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.video:
        manifest_path, script_path = derive_paths(args.video)
    elif args.manifest and args.script:
        manifest_path, script_path = args.manifest, args.script
    else:
        ap.error("provide <manifest> <script>, or --video NN")
        return 2

    rc, verdict, findings = run(manifest_path, script_path)
    line = summary_line(verdict, findings)

    if verdict == "FAIL":
        print(line)
        notify(line + " — see BRANDS/3SK_Finance/Raw_Assets/_manifest_spine_report.md")
    elif not args.quiet_ok:
        print(line)
    if args.verbose and findings:
        for level, check, nm, msg in findings:
            print("  %-4s [%s] %s — %s" % (level, check, nm, msg))

    if args.report_only:
        return 0
    return rc


# --- selftest fixtures (the pickup's binary-review-gate cases) ---------------
_SCRIPT = (
    "Your net worth is negative $27,788. A $612 checking account, minus $28,400 in "
    "student loans. Past $38.4 million by the top. The account grows at 5 percent a year. "
    "Roughly seventy percent of family wealth is gone by generation two."
)


def _m(images):
    return {"project": "selftest", "images": images}


def selftest():
    cases = []  # (label, manifest, script, want_fail_checks, want_warn_checks)

    # clean card — every figure traces, no QC issues
    cases.append(("clean-card", _m([
        {"name": "V_Shot_01b", "prompt": "No character. Flat 2D card reading \"$27,788\" and "
         "\"$612\" and \"$28,400\", brand-red accent, no realistic shading."}]),
        _SCRIPT, set(), set()))

    # invented figure — $1,843 is not in the script (the V8 money-loss class) → FAIL
    cases.append(("invented-figure", _m([
        {"name": "V_Shot_02b", "prompt": "No character. Flat 2D card reading \"$1,843\" in "
         "brand-red, no realistic shading."}]),
        _SCRIPT, {"spine-figure"}, set()))

    # 38.4M ↔ "$38.4 million" normalization — must PASS (both cores == 38.4)
    cases.append(("million-normalization", _m([
        {"name": "V_Shot_03b", "prompt": "No character. Flat card reading \"$38.4\" at the top, "
         "no realistic shading."}]),
        _SCRIPT, set(), set()))

    # sanctioned negation — "NOT realistic or cinematic" must NOT flag banned vocab
    cases.append(("sanctioned-negation", _m([
        {"name": "V_Shot_04a", "prompt": "Three at a desk. Flat 2D, NOT realistic or cinematic, "
         "no soft lighting."}]),
        _SCRIPT, set(), set()))

    # prefix negation — "undramatic" is the house style, must NOT flag banned
    # vocab. Regression pin for the 2026-07-22 false FAIL on V13's Shot_23a
    # ("his expression is calm and entirely undramatic"), which blocked a clean
    # manifest that had already passed its PROMPTS review.
    cases.append(("prefix-negation", _m([
        {"name": "V_Shot_06a", "prompt": "Three at a window. Flat 2D. His expression is calm "
         "and entirely undramatic. Stylized flat lighting only."}]),
        _SCRIPT, set(), set()))

    # ...but a prefix negation must NOT license a genuinely un-negated later use
    cases.append(("prefix-negation-does-not-leak", _m([
        {"name": "V_Shot_06b", "prompt": "Calm and undramatic throughout. Then a dramatic "
         "push-in on the ladder."}]),
        _SCRIPT, {"banned-vocab"}, set()))

    # A hard wrap must not turn the anchor into a negation. The second clause
    # breaks the line mid-word after "un", which is the ONLY lookback shape that
    # still reaches the `$`-vs-`\Z` distinction now that `a|in` are dropped —
    # so this fixture uniquely dies if `\Z` is reverted to `$`. (An earlier
    # version used "Then a\ndramatic" and survived that mutation: with `a` gone
    # from the alternation the lookback was unreachable, so it pinned the
    # alternation change rather than the anchor it claimed to.) 14 of 1,608
    # corpus prompts contain literal newlines, so the shape is real.
    cases.append(("prefix-negation-newline-not-a-prefix", _m([
        {"name": "V_Shot_06c", "prompt": "Calm and undramatic.\nFlat, un\ndramatic push-in."}]),
        _SCRIPT, {"banned-vocab"}, set()))

    # spaced acronym in a card → WARN (tts-spacing)
    cases.append(("spaced-acronym", _m([
        {"name": "V_Shot_05b", "prompt": "No character. Flat card reading \"P M I DROPS AT "
         "$28,400\", no realistic shading."}]),
        _SCRIPT, set(), {"tts-spacing"}))

    # thumbnail with no reserved title zone → WARN
    cases.append(("thumb-no-zone", _m([
        {"name": "V_Thumbnail_A", "prompt": "No character. Bold red card, big number $38.4 "
         "centered, no realistic shading."}]),
        _SCRIPT, set(), {"thumb-title-zone"}))

    # passive uppercase reserved zone — "KEPT CLEAR AND EMPTY for a title overlay" → must NOT flag
    cases.append(("thumb-passive-zone", _m([
        {"name": "V_Thumbnail_B", "prompt": "No character. Top third KEPT CLEAR AND EMPTY for a "
         "title overlay; number $38.4 below, no realistic shading."}]),
        _SCRIPT, set(), set()))

    # duplicate names → FAIL (structural)
    cases.append(("dup-names", _m([
        {"name": "V_Shot_06b", "prompt": "No character. Card \"$612\", no realistic shading."},
        {"name": "V_Shot_06b", "prompt": "No character. Card \"$612\", no realistic shading."}]),
        _SCRIPT, {"structural"}, set()))

    # percent spelled out — card "70%" vs script "seventy percent" → WARN (not FAIL)
    cases.append(("spelled-percent", _m([
        {"name": "V_Shot_07b", "prompt": "No character. Card reading \"70% GONE BY GEN 2\", "
         "no realistic shading."}]),
        _SCRIPT, set(), {"spine-figure"}))

    # numeric percent that IS in the script ("5 percent") — card "5%" must NOT flag
    cases.append(("numeric-percent-ok", _m([
        {"name": "V_Shot_08b", "prompt": "No character. Card reading \"5% A YEAR\", "
         "no realistic shading."}]),
        _SCRIPT, set(), set()))

    # banned vocab with no negation → FAIL
    cases.append(("banned-unnegated", _m([
        {"name": "V_Shot_09a", "prompt": "Three at a window, cinematic lighting, warm glow."}]),
        _SCRIPT, {"banned-vocab"}, set()))

    ok = True
    for label, manifest, script, want_fail, want_warn in cases:
        findings = lint(manifest, script)
        got_fail = {f[1] for f in findings if f[0] == "FAIL"}
        got_warn = {f[1] for f in findings if f[0] == "WARN"}
        # every wanted check present; no unexpected FAIL (WARN over-reporting is tolerated
        # only when not asserting a clean case)
        fail_ok = want_fail <= got_fail and got_fail <= want_fail
        warn_ok = want_warn <= got_warn
        clean_warn_ok = (got_warn == want_warn) if not want_fail else True
        if not (fail_ok and warn_ok and clean_warn_ok):
            ok = False
            print("  FAIL fixture '%s': want_fail=%s got_fail=%s | want_warn=%s got_warn=%s"
                  % (label, sorted(want_fail), sorted(got_fail), sorted(want_warn), sorted(got_warn)))

    if ok:
        print("manifest_spine_lint --selftest: PASS (%d fixtures)" % len(cases))
        return 0
    print("manifest_spine_lint --selftest: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
