#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Agent Forge — asciinema demo script
# Simulates a realistic agent run with typewriter-styled output.
# Requires: bash 4+, no external deps.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colours & Formatting ────────────────────────────────────
RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
CYAN="\033[36m"
YELLOW="\033[33m"
MAGENTA="\033[35m"
BLUE="\033[34m"
WHITE="\033[97m"
BG_BLUE="\033[44m"

# ── Timing ──────────────────────────────────────────────────
TYPING_DELAY=0.03      # seconds per character
LINE_PAUSE=0.08        # pause between output lines
SECTION_PAUSE=1.0      # pause between major sections
PROMPT_PAUSE=0.6       # pause before typing starts

# ── Helpers ─────────────────────────────────────────────────
type_text() {
    # Print text one character at a time (typewriter effect)
    local text="$1"
    for (( i=0; i<${#text}; i++ )); do
        printf '%s' "${text:$i:1}"
        sleep "$TYPING_DELAY"
    done
}

type_command() {
    # Simulate typing a command at a prompt
    printf "${GREEN}${BOLD}❯${RESET} "
    sleep "$PROMPT_PAUSE"
    type_text "$1"
    printf '\n'
    sleep 0.4
}

print_line() {
    printf '%b\n' "$1"
    sleep "$LINE_PAUSE"
}

section_break() {
    sleep "$SECTION_PAUSE"
}

# ── Clear screen ────────────────────────────────────────────
clear
sleep 0.5

# ── Banner ──────────────────────────────────────────────────
printf "${BOLD}${CYAN}"
cat << 'BANNER'
    _                    _     _____
   / \   __ _  ___ _ __ | |_  |  ___|__  _ __ __ _  ___
  / _ \ / _` |/ _ \ '_ \| __| | |_ / _ \| '__/ _` |/ _ \
 / ___ \ (_| |  __/ | | | |_  |  _| (_) | | | (_| |  __/
/_/   \_\__, |\___|_| |_|\__| |_|  \___/|_|  \__, |\___|
        |___/                                 |___/
BANNER
printf "${RESET}"
print_line ""
print_line "${DIM}  A sandboxed AI coding agent runtime${RESET}"
print_line ""
sleep 1.5

# ── Command 1: Show help ────────────────────────────────────
type_command "agent-forge --help"

print_line ""
print_line "${BOLD}Usage:${RESET} agent-forge [OPTIONS] COMMAND [ARGS]..."
print_line ""
print_line "  ${CYAN}Agent Forge${RESET} — autonomous coding agent with sandboxed tool execution."
print_line ""
print_line "${BOLD}Commands:${RESET}"
print_line "  ${GREEN}run${RESET}      Run an agent task on a repository"
print_line "  ${GREEN}status${RESET}   Check the status of an agent run"
print_line "  ${GREEN}list${RESET}     List recent agent runs"
print_line "  ${GREEN}config${RESET}   Show resolved configuration"
print_line ""

section_break

# ── Command 2: Run the agent ────────────────────────────────
type_command "agent-forge run \\"
printf "    "
type_text "--task \"Fix the health endpoint — it returns 500 instead of 200\" \\"
printf '\n'
sleep 0.1
printf "    "
type_text "--repo ./sample-app"
printf '\n'
sleep 0.6

print_line ""
print_line "${BOLD}${BLUE}🔧 Agent Forge v0.1.0${RESET}"
print_line "${BOLD}📋 Task:${RESET} Fix the health endpoint — it returns 500 instead of 200"
print_line "${BOLD}🐳 Sandbox:${RESET} agent-forge-sandbox:latest"
print_line "${BOLD}🧠 Model:${RESET} gemini-3.1-flash-lite-preview"
print_line ""

section_break

# ── Iteration 1 ─────────────────────────────────────────────
print_line "${BOLD}${BG_BLUE}${WHITE} ━━━ Iteration 1/25 ━━━ ${RESET}"
sleep 0.3
print_line "${YELLOW}🤔 Thinking:${RESET} I need to read app.py to understand the current health endpoint."
sleep 0.4
print_line "${MAGENTA}🔧 Tool:${RESET} ${BOLD}read_file${RESET}(path=${DIM}\"app.py\"${RESET})"
sleep 0.8
print_line "${GREEN}✅ Read 42 lines${RESET}"
print_line ""

section_break

# ── Iteration 2 ─────────────────────────────────────────────
print_line "${BOLD}${BG_BLUE}${WHITE} ━━━ Iteration 2/25 ━━━ ${RESET}"
sleep 0.3
print_line "${YELLOW}🤔 Thinking:${RESET} Found the bug on line 28 — \`return jsonify({\"status\": \"ok\"}), 500\`."
sleep 0.3
print_line "   The status code should be 200, not 500."
sleep 0.4
print_line "${MAGENTA}🔧 Tool:${RESET} ${BOLD}edit_file${RESET}(path=${DIM}\"app.py\"${RESET}, target=${DIM}\"500\"${RESET}, replacement=${DIM}\"200\"${RESET})"
sleep 0.8
print_line "${GREEN}✅ Applied 1 edit (line 28)${RESET}"
print_line ""

section_break

# ── Iteration 3 ─────────────────────────────────────────────
print_line "${BOLD}${BG_BLUE}${WHITE} ━━━ Iteration 3/25 ━━━ ${RESET}"
sleep 0.3
print_line "${YELLOW}🤔 Thinking:${RESET} Let me verify the fix by running the test suite."
sleep 0.4
print_line "${MAGENTA}🔧 Tool:${RESET} ${BOLD}run_shell${RESET}(command=${DIM}\"python -m pytest test_app.py -v\"${RESET})"
sleep 1.2
print_line "${DIM}test_app.py::test_greet ................. PASSED${RESET}"
print_line "${DIM}test_app.py::test_health_returns_200 .... PASSED${RESET}"
print_line "${DIM}test_app.py::test_health_json ........... PASSED${RESET}"
sleep 0.3
print_line "${GREEN}✅ 3 passed${RESET} in 0.42s"
print_line ""

section_break

# ── Run Summary ─────────────────────────────────────────────
print_line "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
print_line "${BOLD}${GREEN}✅ Run Complete${RESET}"
print_line "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
print_line ""
print_line "  ${BOLD}Status:${RESET}      ${GREEN}COMPLETED${RESET}"
print_line "  ${BOLD}Iterations:${RESET}  3 / 25"
print_line "  ${BOLD}Tokens:${RESET}      1,847 ${DIM}(in: 1,203 / out: 644)${RESET}"
print_line "  ${BOLD}Cost:${RESET}        \$0.0008"
print_line "  ${BOLD}Duration:${RESET}    12.3s"
print_line "  ${BOLD}Run ID:${RESET}      ${DIM}a1b2c3d4-e5f6-7890-abcd-ef1234567890${RESET}"
print_line ""
sleep 2.0

# ── Command 3: Check status ─────────────────────────────────
type_command "agent-forge list --limit 3"
print_line ""
print_line "${BOLD}Recent Runs${RESET}"
print_line "${DIM}──────────────────────────────────────────────────────────────────────${RESET}"
print_line "  ${GREEN}✅${RESET} a1b2c3d4  Fix the health endpoint            3 iters   12.3s   \$0.0008"
print_line "  ${GREEN}✅${RESET} f8e7d6c5  Add input validation to /greet     5 iters   18.7s   \$0.0012"
print_line "  ${GREEN}✅${RESET} 98765432  Add type hints to utils.py         4 iters   15.1s   \$0.0010"
print_line ""

sleep 2.0
print_line "${DIM}Thanks for watching! ⭐ github.com/akoita/agent-forge${RESET}"
print_line ""
