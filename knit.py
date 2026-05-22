#!/usr/bin/env python3
"""Knitting Compiler - A DSL compiler for knitting patterns."""

import json
import re
import sys


KEYWORDS = ["pattern", "cast_on", "row", "repeat", "bind_off"]
SIMPLE_STITCHES = {"yo", "k2tog", "ssk", "inc", "dec"}

STITCH_CONSUME = {"k": 1, "p": 1, "yo": 0, "k2tog": 2, "ssk": 2, "inc": 1, "dec": 2}
STITCH_PRODUCE = {"k": 1, "p": 1, "yo": 1, "k2tog": 1, "ssk": 1, "inc": 2, "dec": 1}

ERROR_ORDER = [
    "MISSING_PATTERN", "MALFORMED_PATTERN", "DUPLICATE_PATTERN",
    "MISSING_CAST_ON", "MALFORMED_CAST_ON", "DUPLICATE_CAST_ON", "CAST_ON_OUT_OF_ORDER",
    "UNKNOWN_STATEMENT",
    "MALFORMED_ROW", "DUPLICATE_ROW", "OUT_OF_ORDER_ROW",
    "UNKNOWN_STITCH",
    "STITCH_UNDERFLOW", "STITCH_OVERFLOW",
    "MALFORMED_REPEAT", "INVALID_REPEAT_COUNT", "INVALID_REPEAT_RANGE",
    "MALFORMED_BIND_OFF", "DUPLICATE_BIND_OFF", "BIND_OFF_OUT_OF_ORDER",
]
ERROR_ORDER_IDX = {c: i for i, c in enumerate(ERROR_ORDER)}


def strip_comments(line):
    in_quotes = False
    out = []
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
        if ch == '#' and not in_quotes:
            break
        out.append(ch)
    return ''.join(out).rstrip('\n').rstrip('\r')


def classify_line(stripped):
    for kw in KEYWORDS:
        if stripped == kw:
            return kw, ''
        if stripped.startswith(kw):
            n = len(kw)
            if n < len(stripped) and stripped[n] in ' \t':
                return kw, stripped[n:].strip()
    return None, None


def parse_pattern_name(stripped, line_num):
    rest = stripped[len("pattern"):].strip()
    if not rest.startswith('"'):
        return None, make_error("MALFORMED_PATTERN", "Malformed pattern declaration.", line_num, None)
    end = rest.find('"', 1)
    if end == -1:
        return None, make_error("MALFORMED_PATTERN", "Unclosed pattern name.", line_num, None)
    name = rest[1:end]
    trailing = rest[end + 1:].strip()
    if trailing:
        return None, make_error("MALFORMED_PATTERN", "Trailing text after pattern name.", line_num, None)
    return name, None


def make_error(code, message, line, row):
    return {"type": "error", "code": code, "message": message, "line": line, "row": row}


def error_sort_key(err):
    line = err["line"] if err["line"] is not None else 10**9
    code_idx = ERROR_ORDER_IDX.get(err["code"], 999)
    return (line, code_idx)


class Compiler:
    def __init__(self):
        self.errors = []
        self.pattern_name = None
        self.cast_on_val = None
        self.has_duplicate_pattern = False
        self.has_valid_pattern = False
        self.has_valid_cast_on = False
        self.cast_on_line = None
        self.valid_cast_on_count = 0
        self.bind_off_found = False
        self.bind_off_line = None
        self.valid_bind_off_count = 0
        self.stmts = []
        self.rows = {}
        self.row_order = []
        self.max_claimed_row = 0
        self.seen_row_numbers = set()
        self.first_row_line = None
        self.has_rows = False
        self.cast_on_after_rows = False
        self.cast_on_after_rows_line = None

    def add_error(self, err):
        self.errors.append(err)

    def run(self, filepath):
        try:
            with open(filepath, 'r') as f:
                raw_lines = f.readlines()
        except Exception as e:
            print(json.dumps({
                "pattern_name": None, "cast_on": None, "valid": False,
                "errors": [make_error("MALFORMED_CAST_ON", str(e), None, None)],
                "expanded_rows": [], "final_stitch_count": None, "bind_off": False
            }))
            return 1

        physical_lines = []
        for i, line in enumerate(raw_lines, 1):
            clean = strip_comments(line).strip()
            physical_lines.append((i, clean))

        self.parse_all(physical_lines)
        self.validate()
        usable_co = self.get_usable_cast_on()
        if usable_co is not None:
            expanded = self.expand_and_simulate(usable_co)
        else:
            expanded = None

        out = self.build_output(usable_co, expanded)
        print(json.dumps(out))
        if self.errors:
            return 1
        return 0

    def parse_all(self, physical_lines):
        for line_num, stripped in physical_lines:
            if not stripped:
                continue
            kw, rest = classify_line(stripped)
            if kw is None:
                self.add_error(make_error("UNKNOWN_STATEMENT", f"Unknown statement on line {line_num}.", line_num, None))
                continue
            self.stmts.append((kw, rest, line_num, stripped))
            after_bind_off = self.valid_bind_off_count > 0
            if kw == "pattern":
                self._parse_pattern(rest, line_num)
            elif kw == "cast_on":
                self._parse_cast_on(rest, line_num)
            elif kw == "row":
                self._parse_row(rest, line_num, stripped)
            elif kw == "repeat":
                self._parse_repeat(rest, line_num)
            elif kw == "bind_off":
                self._parse_bind_off(rest, line_num)
            if after_bind_off and kw != "bind_off":
                rn = None
                if kw == "row":
                    colon_pos = rest.find(':')
                    if colon_pos != -1:
                        rn_str = rest[:colon_pos].strip()
                        if re.match(r'^\d+$', rn_str):
                            rn = int(rn_str)
                self.add_error(make_error("BIND_OFF_OUT_OF_ORDER", "Statement after bind_off.", line_num, rn))

    def _parse_pattern(self, rest, line_num):
        if not rest:
            self.add_error(make_error("MALFORMED_PATTERN", "Malformed pattern declaration.", line_num, None))
            return
        name, err = parse_pattern_name(f"pattern {rest}", line_num)
        if err:
            self.add_error(err)
            return
        if self.has_valid_pattern:
            self.has_duplicate_pattern = True
            self.add_error(make_error("DUPLICATE_PATTERN", "Duplicate pattern declaration.", line_num, None))
            return
        self.pattern_name = name
        self.has_valid_pattern = True

    def _parse_cast_on(self, rest, line_num):
        if self.has_rows and self.first_row_line is not None and line_num > self.first_row_line:
            self.cast_on_after_rows = True
            if self.cast_on_after_rows_line is None:
                self.cast_on_after_rows_line = line_num
        if not rest:
            self.add_error(make_error("MALFORMED_CAST_ON", "Malformed cast_on.", line_num, None))
            return
        if not re.match(r'^\d+$', rest) or int(rest) <= 0:
            self.add_error(make_error("MALFORMED_CAST_ON", f"Invalid cast_on value '{rest}'.", line_num, None))
            return
        val = int(rest)
        if self.has_valid_cast_on:
            self.add_error(make_error("DUPLICATE_CAST_ON", "Duplicate cast_on.", line_num, None))
            return
        self.cast_on_val = val
        self.has_valid_cast_on = True
        self.cast_on_line = line_num
        self.valid_cast_on_count += 1

    def _parse_row(self, rest, line_num, stripped):
        if not self.has_rows:
            self.first_row_line = line_num
            self.has_rows = True
        if not rest:
            self.add_error(make_error("MALFORMED_ROW", "Malformed row statement.", line_num, None))
            return
        colon_pos = rest.find(':')
        if colon_pos == -1:
            self.add_error(make_error("MALFORMED_ROW", "Row missing colon.", line_num, None))
            return
        row_num_str = rest[:colon_pos].strip()
        instr_part = rest[colon_pos + 1:].strip()
        if not re.match(r'^\d+$', row_num_str) or int(row_num_str) <= 0:
            self.add_error(make_error("MALFORMED_ROW", f"Invalid row number '{row_num_str}'.", line_num, None))
            rn = None
            claims = False
        else:
            rn = int(row_num_str)
            claims = True

        parsed_instrs, instr_errors = self.parse_instruction_list(instr_part, line_num, rn if claims else None)

        if rn is not None and claims:
            if rn in self.seen_row_numbers:
                self.add_error(make_error("DUPLICATE_ROW", f"Duplicate row number {rn}.", line_num, rn))
                claims = False
            elif self.max_claimed_row > 0 and rn < self.max_claimed_row:
                self.add_error(make_error("OUT_OF_ORDER_ROW", f"Out of order row {rn}.", line_num, rn))
                self.seen_row_numbers.add(rn)
                claims = False
            else:
                self.max_claimed_row = rn

            if claims:
                self.seen_row_numbers.add(rn)
                self.rows[rn] = {
                    "line": line_num,
                    "instructions_raw": instr_part,
                    "parsed": parsed_instrs,
                    "sim_error": None,
                }
                self.row_order.append(rn)

        for e in instr_errors:
            row_val = rn if rn is not None and rn > 0 else None
            if rn is not None and rn == 0:
                row_val = None
            self.add_error(make_error(e["code"], e["message"], line_num, row_val))

        has_malformed = any(e["code"] == "MALFORMED_ROW" for e in instr_errors)
        has_unknown_stitch = any(e["code"] == "UNKNOWN_STITCH" for e in instr_errors)
        if claims and rn in self.rows and (has_malformed or has_unknown_stitch):
            self.rows[rn]["skip_sim"] = True
        elif claims and rn in self.rows:
            self.rows[rn]["skip_sim"] = False

    def split_top_level(self, text):
        parts = []
        depth = 0
        current = []
        for ch in text:
            if ch == '[':
                depth += 1
                current.append(ch)
            elif ch == ']':
                depth -= 1
                current.append(ch)
            elif ch == ',' and depth == 0:
                parts.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        parts.append(''.join(current).strip())
        return parts

    def parse_instruction_list(self, text, line_num, row_num):
        if not text:
            return [], [make_error("MALFORMED_ROW", "Empty instruction list.", line_num, row_num)]

        parts = self.split_top_level(text)
        errors = []
        has_empty = False
        has_whitespace_glue = False
        parsed = []

        for p in parts:
            if not p:
                has_empty = True
                continue
            if re.search(r'\s', p) and not p.startswith('['):
                has_whitespace_glue = True
                continue
            result = self.parse_instruction_item(p, line_num, row_num)
            if isinstance(result, list):
                has_err = False
                for item in result:
                    if isinstance(item, dict) and "code" in item:
                        has_err = True
                        errors.append(item)
                if has_err:
                    parsed.append(None)
                else:
                    parsed.extend(result)
            elif result is None:
                pass
            else:
                parsed.append(result)

        if has_empty:
            errors.append(make_error("MALFORMED_ROW", "Empty item in instruction list.", line_num, row_num))
        if has_whitespace_glue:
            errors.append(make_error("MALFORMED_ROW", "Instructions not properly comma-separated.", line_num, row_num))

        has_malformed = has_empty or has_whitespace_glue

        return parsed, errors

    def parse_instruction_item(self, item, line_num, row_num, detect_only=False):
        item = item.strip()
        if not item:
            return None
        if item.startswith('['):
            return self.parse_bracket_repeat(item, line_num, row_num)
        m = re.match(r'^(k|p)(\d+)$', item)
        if m:
            stitch = m.group(1)
            count = int(m.group(2))
            if count <= 0:
                return [make_error("UNKNOWN_STITCH", f"Invalid stitch '{item}'.", line_num, row_num)]
            if detect_only:
                return None
            return {"stitch": stitch, "count": count}
        if item in SIMPLE_STITCHES:
            if detect_only:
                return None
            return {"stitch": item, "count": 1}
        return [make_error("UNKNOWN_STITCH", f"Unknown stitch '{item}'.", line_num, row_num)]

    def parse_bracket_repeat(self, text, line_num, row_num):
        if not text.startswith('['):
            return [make_error("MALFORMED_ROW", "Expected bracket repeat.", line_num, row_num)]
        depth = 0
        end = -1
        for i, ch in enumerate(text):
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return [make_error("MALFORMED_ROW", "Unclosed bracket.", line_num, row_num)]

        content = text[1:end]
        after = text[end + 1:]

        m = re.match(r'^\s+x(\d+)$', after)
        if not m:
            return [make_error("MALFORMED_ROW", "Invalid bracket repeat syntax.", line_num, row_num)]
        count = int(m.group(1))
        if count <= 0:
            return [make_error("MALFORMED_ROW", "Bracket repeat count must be positive.", line_num, row_num)]

        inner_parts = self.split_top_level(content)
        inner_items = []
        for p in inner_parts:
            if not p:
                return [make_error("MALFORMED_ROW", "Empty item in bracket repeat.", line_num, row_num)]
            if re.search(r'\s', p) and not p.startswith('['):
                return [make_error("MALFORMED_ROW", "Bracket content not comma-separated.", line_num, row_num)]
            inner = self.parse_instruction_item(p, line_num, row_num, detect_only=False)
            if inner is None:
                return [make_error("MALFORMED_ROW", "Invalid bracket content.", line_num, row_num)]
            if isinstance(inner, list):
                for ii in inner:
                    if isinstance(ii, dict) and "code" in ii:
                        return [ii]
            if isinstance(inner, dict) and "code" in inner:
                return [inner]
            if isinstance(inner, list):
                inner_items.extend(inner)
            else:
                inner_items.append(inner)

        result = []
        for _ in range(count):
            for item in inner_items:
                if isinstance(item, dict) and "stitch" in item:
                    result.append(dict(item))
                else:
                    result.append(item)
        return result

    def _parse_repeat(self, rest, line_num):
        if not rest:
            self.add_error(make_error("MALFORMED_REPEAT", "Malformed repeat statement.", line_num, None))
            return
        if not rest.startswith("rows "):
            self.add_error(make_error("MALFORMED_REPEAT", "Repeat missing 'rows' keyword.", line_num, None))
            return
        rest2 = rest[5:].strip()
        m = re.match(r'^(\d+)-(\d+)\s+x(\d+)$', rest2)
        if not m:
            m2 = re.match(r'^(\d+)-(\d+)\s+x$', rest2)
            if m2:
                start, end = int(m2.group(1)), int(m2.group(2))
                err = make_error("INVALID_REPEAT_COUNT", "Missing repeat count.", line_num, None)
                self.add_error(err)
                if start <= 0 or end <= 0 or start > end:
                    self.add_error(make_error("INVALID_REPEAT_RANGE", f"Invalid repeat range {start}-{end}.", line_num, None))
                elif start > self.max_claimed_row or end > self.max_claimed_row:
                    self.add_error(make_error("INVALID_REPEAT_RANGE", f"Repeat range {start}-{end} references non-existent rows.", line_num, None))
                return
            m3 = re.match(r'^(\d+)-(\d+)\s+x(.+)$', rest2)
            if m3:
                start, end, count_str = int(m3.group(1)), int(m3.group(2)), m3.group(3).strip()
                err = make_error("INVALID_REPEAT_COUNT", f"Invalid repeat count '{count_str}'.", line_num, None)
                self.add_error(err)
                if start <= 0 or end <= 0 or start > end:
                    self.add_error(make_error("INVALID_REPEAT_RANGE", f"Invalid repeat range {start}-{end}.", line_num, None))
                elif start > self.max_claimed_row or end > self.max_claimed_row:
                    self.add_error(make_error("INVALID_REPEAT_RANGE", f"Repeat range {start}-{end} references non-existent rows.", line_num, None))
                return
            self.add_error(make_error("MALFORMED_REPEAT", "Malformed repeat structure.", line_num, None))
            return

        start, end, count = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if count <= 0:
            self.add_error(make_error("INVALID_REPEAT_COUNT", f"Non-positive repeat count {count}.", line_num, None))
            range_bad = False
            if start <= 0 or end <= 0 or start > end:
                range_bad = True
            elif start > self.max_claimed_row or end > self.max_claimed_row:
                range_bad = True
            if range_bad:
                self.add_error(make_error("INVALID_REPEAT_RANGE", f"Invalid repeat range {start}-{end}.", line_num, None))
            return
        range_valid = True
        range_reasons = []
        if start <= 0 or end <= 0:
            range_valid = False
            range_reasons.append("non-positive")
        if start > end:
            range_valid = False
            range_reasons.append("start > end")
        if not range_valid:
            self.add_error(make_error("INVALID_REPEAT_RANGE", f"Invalid repeat range {start}-{end}.", line_num, None))
            return
        if start > self.max_claimed_row or end > self.max_claimed_row:
            self.add_error(make_error("INVALID_REPEAT_RANGE", f"Repeat range {start}-{end} references non-existent rows.", line_num, None))
            return
        self.stmts.append(("_repeat_valid", {"start": start, "end": end, "count": count, "line": line_num}, line_num, None))

    def _parse_bind_off(self, rest, line_num):
        if rest:
            self.add_error(make_error("MALFORMED_BIND_OFF", "Malformed bind_off.", line_num, None))
            if self.valid_bind_off_count > 0:
                self.add_error(make_error("BIND_OFF_OUT_OF_ORDER", "Statement after bind_off.", line_num, None))
            return
        if self.valid_bind_off_count > 0:
            self.add_error(make_error("DUPLICATE_BIND_OFF", "Duplicate bind_off.", line_num, None))
            return
        self.bind_off_found = True
        self.bind_off_line = line_num
        self.valid_bind_off_count += 1

    def validate(self):
        if not self.has_valid_pattern and not self.has_duplicate_pattern:
            if not any(e["code"] == "MALFORMED_PATTERN" for e in self.errors):
                self.add_error(make_error("MISSING_PATTERN", "Missing pattern declaration.", None, None))
        if self.cast_on_after_rows:
            if not any(e["code"] == "CAST_ON_OUT_OF_ORDER" for e in self.errors):
                cl = self.cast_on_after_rows_line if self.cast_on_after_rows_line is not None else self.cast_on_line
                self.add_error(make_error("CAST_ON_OUT_OF_ORDER", "cast_on appears after rows.", cl, None))
        if not self.has_valid_cast_on:
            if self.valid_cast_on_count == 0:
                if not any(e["code"] == "MALFORMED_CAST_ON" for e in self.errors):
                    self.add_error(make_error("MISSING_CAST_ON", "Missing cast_on declaration.", None, None))

    def get_usable_cast_on(self):
        if not self.has_valid_cast_on:
            return None
        if self.valid_cast_on_count > 1:
            return None
        if self.has_valid_cast_on and self.cast_on_after_rows:
            return None
        for e in self.errors:
            if e["code"] in ("DUPLICATE_CAST_ON", "CAST_ON_OUT_OF_ORDER"):
                if e["code"] == "CAST_ON_OUT_OF_ORDER" and self.has_valid_cast_on and not self.cast_on_after_rows:
                    continue
                if e["code"] == "DUPLICATE_CAST_ON":
                    return None
        return self.cast_on_val

    def expand_and_simulate(self, cast_on):
        stmts_parsed = [(kw, data, ln) for kw, data, ln, _ in self.stmts]

        row_sequence = []
        for kw, data, ln in stmts_parsed:
            if kw == "bind_off":
                break
            if kw == "row":
                colon_pos = data.find(':')
                if colon_pos == -1:
                    continue
                rn_str = data[:colon_pos].strip()
                if not re.match(r'^\d+$', rn_str):
                    continue
                rn = int(rn_str)
                if rn in self.seen_row_numbers and rn in self.rows:
                    row_sequence.append(rn)
            elif kw == "_repeat_valid":
                for _ in range(data["count"]):
                    for rn in range(data["start"], data["end"] + 1):
                        if rn in self.seen_row_numbers and rn in self.rows:
                            row_sequence.append(rn)

        stitch_count = cast_on
        expanded_rows_out = []
        sim_errors = []
        sim_stopped = False
        idx = 0

        for rn in row_sequence:
            if sim_stopped:
                break
            row = self.rows.get(rn)
            if row is None:
                continue
            if row.get("skip_sim", False):
                expanded_rows_out.append(None)
                continue
            instrs = row.get("parsed", [])
            if not instrs:
                expanded_rows_out.append(None)
                continue

            idx += 1
            start = stitch_count
            remaining = stitch_count
            produced = 0
            flat_instrs = self.flatten_instructions(instrs)

            sim_error = None
            current_row_stitches = 0
            for instr in flat_instrs:
                if not isinstance(instr, dict) or "stitch" not in instr:
                    continue
                sname = instr["stitch"]
                scnt = instr["count"]

                if sname in ("k", "p"):
                    consume = scnt
                    produce = scnt
                else:
                    consume = STITCH_CONSUME.get(sname, 0)
                    produce = STITCH_PRODUCE.get(sname, 0)

                if remaining < consume:
                    sim_error = make_error("STITCH_UNDERFLOW", f"Row {rn} stitch underflow.", row["line"], rn)
                    sim_stopped = True
                    break
                remaining -= consume
                current_row_stitches += produce

                if current_row_stitches > 10000:
                    sim_error = make_error("STITCH_OVERFLOW", f"Row {rn} stitch overflow.", row["line"], rn)
                    sim_stopped = True
                    break

            if sim_error:
                sim_errors.append(sim_error)
                expanded_rows_out.append(None)
                break

            stitch_count = current_row_stitches
            end = stitch_count

            expanded_rows_out.append({
                "expanded_row_index": idx,
                "source_row": rn,
                "instructions": flat_instrs,
                "start_stitches": start,
                "end_stitches": end,
            })

        for e in sim_errors:
            self.add_error(e)

        if self.errors:
            return None

        return expanded_rows_out

    def flatten_instructions(self, instrs):
        result = []
        for i in instrs:
            if isinstance(i, dict) and "stitch" in i:
                result.append(i)
            elif isinstance(i, list):
                result.extend(self.flatten_instructions(i))
        return result

    def build_output(self, usable_co, expanded):
        if self.has_duplicate_pattern:
            pn = None
        elif not self.has_valid_pattern:
            pn = None
        else:
            pn = self.pattern_name

        if self.valid_cast_on_count > 1:
            cv = None
        elif not self.has_valid_cast_on:
            cv = None
        elif any(e["code"] == "DUPLICATE_CAST_ON" for e in self.errors):
            cv = None
        else:
            cv = self.cast_on_val

        if self.errors:
            return {
                "pattern_name": pn,
                "cast_on": cv,
                "valid": False,
                "errors": sorted(self.errors, key=error_sort_key),
                "expanded_rows": [],
                "final_stitch_count": None,
                "bind_off": self.valid_bind_off_count > 0,
            }

        rows_out = expanded if expanded is not None else []
        if rows_out:
            fc = rows_out[-1]["end_stitches"]
        elif usable_co is not None:
            fc = usable_co
        else:
            fc = None

        return {
            "pattern_name": pn,
            "cast_on": cv,
            "valid": True,
            "errors": [],
            "expanded_rows": rows_out,
            "final_stitch_count": fc,
            "bind_off": self.valid_bind_off_count > 0,
        }


def main():
    if len(sys.argv) < 2:
        sys.exit(2)
    if sys.argv[1] != "compile":
        sys.exit(2)
    if len(sys.argv) != 3:
        sys.exit(2)

    filepath = sys.argv[2]
    comp = Compiler()
    try:
        rc = comp.run(filepath)
        sys.exit(rc)
    except Exception as e:
        print(json.dumps({
            "pattern_name": None, "cast_on": None, "valid": False,
            "errors": [{"type": "error", "code": "MALFORMED_CAST_ON", "message": str(e), "line": None, "row": None}],
            "expanded_rows": [], "final_stitch_count": None, "bind_off": False,
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
