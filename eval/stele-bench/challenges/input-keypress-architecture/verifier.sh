#!/bin/sh
set -eu

mkdir -p /logs/verifier
answer=/workspace/.submission/answer.md
normalized=/tmp/answer.normalized
global_gate=0
manual_focus=0
char_order=0
duplicated_routing=0
source_evidence=0

if [ -f "$answer" ] && [ ! -L "$answer" ] && [ -s "$answer" ] && \
   [ "$(wc -c < "$answer")" -le 12000 ]; then
  # The answer is prose, so related evidence frequently spans Markdown lines.
  # Match semantic clauses across whitespace rather than making line wrapping
  # part of the score.
  LC_ALL=C tr '[:upper:]' '[:lower:]' < "$answer" | tr '\n' ' ' > "$normalized"

  if grep -Eq '(global|app[- ](wide|level))' "$normalized" && \
     grep -Eq '(input|typing).*(mode|state)|focus.*(state|gate|guard)' "$normalized" && \
     grep -Eq '(gate|guard|filter|suppress|block|skip|prevent|disable)' "$normalized" && \
     grep -Eq '(keymap|shortcut|action|navigation)' "$normalized"; then
    global_gate=1
  fi

  if grep -Eq '(screen|popup|dialog)' "$normalized" && \
     grep -Eq '(manual|hand[- ]written|per[- ]screen|bespoke|reports?|implements?)' "$normalized" && \
     grep -Eq '(gap|miss|forget|inconsisten|recur|break)' "$normalized"; then
    manual_focus=1
  fi

  if grep -Eq '(char|character).*(insert|buffer)|(insert|buffer).*(char|character)' "$normalized" && \
     grep -Eq '(before|prior to|ahead of).*(keymap|action|navigation)' "$normalized" && \
     grep -Eq '(vim|q[/, ]+h[/, ]+j[/, ]+k|h[/, ]+j[/, ]+k([/, ]+l)?[/, ]+q)' "$normalized" && \
     grep -Eq '(consum|interpret|resolv|dispatch|trigger|eat|interfer|fire).*(action|navigation|quit|move)|(action|navigation|quit|move).*(consum|interpret|resolv|dispatch|trigger|eat|interfer|fire)' "$normalized"; then
    char_order=1
  fi

  if grep -Eq '(duplicat|re[- ]implement|per[- ]screen|scattered|bespoke)' "$normalized" && \
     grep -Eq '(routing|event handl|key handling|character insertion|insertion logic)' "$normalized" && \
     grep -Eq '(systemic|structural|architectur|recur|reintroduc|keeps? (happening|breaking))' "$normalized"; then
    duplicated_routing=1
  fi

  if grep -Eq '(^|[^[:alnum:]_])([[:alnum:]_./-]+\.rs)([^[:alnum:]_]|$)' "$normalized" && \
     grep -Eq '(input_mode_active|sync_input_mode|get_action|is_input_focused|insert_char|handle_key_with_action|is_action_allowed_when_focused)' "$normalized"; then
    source_evidence=1
  fi
fi

points=$((global_gate + manual_focus + char_order + duplicated_routing + source_evidence))
case "$points" in
  5) reward=1 ;;
  4) reward=0.8 ;;
  3) reward=0.6 ;;
  2) reward=0.4 ;;
  1) reward=0.2 ;;
  *) reward=0 ;;
esac

printf '{"reward":%s}\n' "$reward" > /logs/verifier/reward.json
printf '{"global_input_focus_gate":%s,"manual_focus_reporting":%s,"char_before_keymap":%s,"duplicated_routing":%s,"source_evidence":%s}\n' \
  "$global_gate" "$manual_focus" "$char_order" "$duplicated_routing" "$source_evidence" \
  > /logs/verifier/evidence.json
