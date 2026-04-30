"""Render an interactive HTML map of the integration's config-flow.

Walks ``custom_components/philips_sonicare_ble/config_flow.py`` with the
``ast`` module to extract every ``async_step_*`` method and the transitions
out of it (next-step calls, form re-renders, terminal create-entry / abort,
``errors["base"]`` assignments). Joins the result with titles/descriptions
from ``strings.json`` and emits a single self-contained HTML file:

- a Mermaid flowchart of the step graph
- a side panel showing source line range, docstring, transitions, error keys,
  and the localized title/description for the clicked step

Run from the repo root:

    python3 scripts/render_config_flow.py
    xdg-open /tmp/sonicare_config_flow.html
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CFG_PATH = REPO / "custom_components/philips_sonicare_ble/config_flow.py"
STRINGS_PATH = REPO / "custom_components/philips_sonicare_ble/strings.json"
# Write into the repo so the file is reachable from sandboxed browsers
# (Firefox-Snap / Flatpak Chromium can't see the host's /tmp).
OUTPUT = REPO / "docs/config_flow.html"


def _kw(call: ast.Call, name: str):
    """Return the keyword-arg value node for ``name`` on a Call, or None."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _const_str(node) -> str | None:
    """Return the string value of an ast.Constant, else None."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_guard_test(test: ast.AST) -> bool:
    """True if an ``if`` test reads as a defensive guard.

    Matches ``if x is None``, ``if not x``, and disjunctions of those.
    Deliberately does **not** match ``if x is not None`` — that is the
    standard ``user_input`` submit-handler shape, not a guard.
    """
    if isinstance(test, ast.Compare):
        if any(isinstance(op, ast.Is) for op in test.ops):
            if any(isinstance(c, ast.Constant) and c.value is None for c in test.comparators):
                return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.BoolOp):
        return any(_is_guard_test(v) for v in test.values)
    return False


def _extract_method_data(fn: ast.AST) -> dict:
    """Pull the same transitions/errors/guard data out of any method.

    Used both for ``async_step_*`` methods (where we render the result)
    and for plain helpers (where we use the result to expand the calling
    step's edge set transitively — otherwise indirect step calls via
    helpers like ``_esp_bridge_health_check`` would be invisible).
    """
    transitions: set[tuple[str, str]] = set()
    errors_set: set[str] = set()
    helper_calls: set[str] = set()

    parent_of: dict[int, ast.AST] = {}
    for p in ast.walk(fn):
        for c in ast.iter_child_nodes(p):
            parent_of[id(c)] = p

    def _in_guard(node: ast.AST) -> bool:
        cur = node
        while id(cur) in parent_of:
            parent = parent_of[id(cur)]
            if isinstance(parent, ast.If) and parent.test is not cur:
                if _is_guard_test(parent.test):
                    return True
            if parent is fn:
                return False
            cur = parent
        return False

    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr

            if attr.startswith("async_step_") and attr != fn.name:
                target = attr[len("async_step_"):]
                kind = "guard" if _in_guard(node) else "next"
                transitions.add((kind, target))

            elif attr == "async_show_form":
                sid = _const_str(_kw(node, "step_id"))
                target = sid if sid else fn.name[len("async_step_"):] if fn.name.startswith("async_step_") else None
                if target:
                    kind = "rerender" if target == (fn.name[len("async_step_"):] if fn.name.startswith("async_step_") else "") else "form"
                    transitions.add((kind, target))

            elif attr == "async_show_menu":
                opts = _kw(node, "menu_options")
                if isinstance(opts, ast.List):
                    for el in opts.elts:
                        v = _const_str(el)
                        if v:
                            transitions.add(("menu", v))

            elif attr == "async_create_entry":
                transitions.add(("success", "__SUCCESS__"))

            elif attr == "async_abort":
                reason = _const_str(_kw(node, "reason")) or "abort"
                transitions.add(("abort", f"__ABORT__:{reason}"))

            # Calls to other methods on ``self`` — track for transitive expansion.
            elif (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and not attr.startswith("async_step_")
            ):
                helper_calls.add(attr)

        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "errors"
        ):
            v = _const_str(node.value)
            if v:
                errors_set.add(v)

        if isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Call, ast.Name)):
            name = (
                node.exc.func.id
                if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
                else node.exc.id if isinstance(node.exc, ast.Name) else None
            )
            if name and name.endswith("Exception"):
                transitions.add(("raise", name))

    return {
        "transitions": transitions,
        "errors": errors_set,
        "helper_calls": helper_calls,
    }


def parse_flow(src: str) -> dict:
    tree = ast.parse(src)
    classes: list[dict] = []

    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        # Pass 1 — index every method on the class (steps + helpers) so we
        # can resolve indirect calls.
        method_data: dict[str, dict] = {}
        method_lines: dict[str, tuple[int, int]] = {}
        method_doc: dict[str, str] = {}
        for m in cls.body:
            if isinstance(m, (ast.AsyncFunctionDef, ast.FunctionDef)):
                method_data[m.name] = _extract_method_data(m)
                method_lines[m.name] = (m.lineno, m.end_lineno or m.lineno)
                method_doc[m.name] = ast.get_docstring(m) or ""

        # Pass 2 — for every step, propagate transitions from helper methods
        # transitively (one level of indirection covers `_esp_bridge_health
        # _check` → ``async_step_esp_bridge_status``; deeper chains resolve
        # via the BFS).
        def _expand(method_name: str, visited: set[str]) -> set[tuple[str, str]]:
            if method_name in visited or method_name not in method_data:
                return set()
            visited.add(method_name)
            d = method_data[method_name]
            out = set(d["transitions"])
            for helper in d["helper_calls"]:
                out |= _expand(helper, visited)
            return out

        steps: list[dict] = []
        for fn in cls.body:
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not fn.name.startswith("async_step_"):
                continue

            step = fn.name[len("async_step_"):]
            transitions: set[tuple[str, str]] = _expand(fn.name, set())
            errors_set: set[str] = set(method_data[fn.name]["errors"])
            # Also surface error keys raised by helpers — they end up
            # bubbling into the same step's ``errors`` dict.
            for helper in method_data[fn.name]["helper_calls"]:
                if helper in method_data:
                    errors_set |= method_data[helper]["errors"]

            steps.append({
                "name": step,
                "docstring": method_doc.get(fn.name, ""),
                "transitions": sorted(transitions),
                "errors": sorted(errors_set),
                "line_start": method_lines[fn.name][0],
                "line_end": method_lines[fn.name][1],
            })

        if steps:
            classes.append({"class": cls.name, "steps": steps})

    return {"classes": classes}


def load_strings() -> dict:
    """Return strings.json's step→{title, description, errors, …} mapping for the main flow."""
    if not STRINGS_PATH.exists():
        return {}
    raw = json.loads(STRINGS_PATH.read_text())
    out = {
        "config": raw.get("config", {}).get("step", {}),
        "options": raw.get("options", {}).get("step", {}),
        "config_errors": raw.get("config", {}).get("error", {}),
        "config_abort": raw.get("config", {}).get("abort", {}),
    }
    return out


def mermaid_for(class_data: dict, idx: int) -> str:
    """Generate one Mermaid flowchart for one ConfigFlow class.

    Maximally conservative v10 syntax: rectangle nodes only, ASCII labels,
    plain arrows. No edge labels (kind information lives in the side panel
    on click). No click directive — the HTML attaches handlers to the
    rendered SVG nodes directly via the ``data-step-name`` attributes we
    inject after Mermaid finishes its render pass.
    """
    lines = [
        "flowchart TD",
        "  classDef step fill:#e7f0ff,stroke:#3a5fa8,color:#0a1d4d",
        "  classDef terminal fill:#dcecdc,stroke:#3a8a3a,color:#1d4d1d",
        "  classDef abort fill:#f6dada,stroke:#a83a3a,color:#5d1d1d",
    ]

    step_ids: list[str] = []
    for s in class_data["steps"]:
        nid = f"S{idx}_{s['name']}"
        step_ids.append(nid)
        lines.append(f'  {nid}["{s["name"]}"]')

    success_used = any(
        t[0] == "success" for s in class_data["steps"] for t in s["transitions"]
    )
    success_id = f"T{idx}_SUCCESS"
    if success_used:
        lines.append(f'  {success_id}["create_entry"]')

    abort_reasons = {
        t[1].split(":", 1)[1]
        for s in class_data["steps"] for t in s["transitions"]
        if t[0] == "abort"
    }
    abort_ids: list[str] = []
    for reason in sorted(abort_reasons):
        rid = reason.replace("-", "_").replace(" ", "_")
        full_id = f"T{idx}_ABORT_{rid}"
        abort_ids.append(full_id)
        # No colon in label — Mermaid v10's lexer mis-tokenizes ':' inside
        # quoted labels in some contexts (treats the label as if it were
        # spilling into a subsequent edge or class shortcut).
        lines.append(f'  {full_id}["abort {reason}"]')

    # Edges — only solid for next/success/abort, dotted for form/rerender/menu.
    # No edge labels (avoids label-parsing issues on v10).
    for s in class_data["steps"]:
        src = f"S{idx}_{s['name']}"
        for kind, target in s["transitions"]:
            if kind == "next":
                lines.append(f"  {src} --> S{idx}_{target}")
            elif kind == "menu":
                lines.append(f"  {src} -.-> S{idx}_{target}")
            elif kind == "form":
                lines.append(f"  {src} -.-> S{idx}_{target}")
            elif kind == "rerender":
                lines.append(f"  {src} -.-> {src}")
            elif kind == "guard":
                # Defensive fallback inside an "is None"/"not X" guard —
                # not a normal forward transition. Render thin/dashed.
                lines.append(f"  {src} -.-> S{idx}_{target}")
            elif kind == "success":
                lines.append(f"  {src} --> {success_id}")
            elif kind == "abort":
                reason = target.split(":", 1)[1]
                rid = reason.replace("-", "_").replace(" ", "_")
                lines.append(f"  {src} --> T{idx}_ABORT_{rid}")
            # 'raise' edges are recorded as exceptions but not drawn

    # Class assignments — listed last as plain `class` statements.
    if step_ids:
        lines.append(f"  class {','.join(step_ids)} step")
    if success_used:
        lines.append(f"  class {success_id} terminal")
    if abort_ids:
        lines.append(f"  class {','.join(abort_ids)} abort")

    return "\n".join(lines)


def emit_html(flow: dict, strings: dict) -> str:
    classes = flow["classes"]
    diagrams = []
    diagram_sources: list[dict[str, str]] = []
    details_blob: dict[str, dict] = {}

    for idx, cls in enumerate(classes):
        cls_label = cls["class"]
        is_options = "Options" in cls_label
        strings_section = strings.get("options" if is_options else "config", {})
        errors_section = {} if is_options else strings.get("config_errors", {})

        src = mermaid_for(cls, idx)
        diagrams.append({
            "label": cls_label,
            "mermaid": src,
        })
        diagram_sources.append({"label": cls_label, "src": src, "idx": idx})

        for s in cls["steps"]:
            ui = strings_section.get(s["name"], {}) if isinstance(strings_section.get(s["name"], {}), dict) else {}
            details_blob[f"{idx}::{s['name']}"] = {
                "name": s["name"],
                "class": cls_label,
                "docstring": s["docstring"],
                "transitions": [list(t) for t in s["transitions"]],
                "errors": s["errors"],
                "line_start": s["line_start"],
                "line_end": s["line_end"],
                "ui_title": ui.get("title", ""),
                "ui_description": ui.get("description", ""),
                "ui_data": ui.get("data", {}),
                "ui_data_description": ui.get("data_description", {}),
                "error_strings": {
                    e: errors_section.get(e, "") for e in s["errors"]
                },
            }

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Philips Sonicare BLE — Config Flow Map</title>
<style>
  :root {{
    --bg: #fafafa;
    --panel: #ffffff;
    --border: #d6d6d6;
    --muted: #5e6b7a;
    --accent: #3a5fa8;
    --code-bg: #f4f6fa;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: #1a1a1a; }}
  header {{ padding: 12px 20px; background: #1d2b4a; color: white; display: flex; align-items: center; gap: 16px; }}
  header h1 {{ margin: 0; font-size: 18px; font-weight: 500; }}
  header .meta {{ margin-left: auto; font-size: 12px; opacity: 0.8; }}
  main {{ display: grid; grid-template-columns: 1fr 420px; height: calc(100vh - 50px); }}
  #diagrams {{ overflow: auto; padding: 16px; background: var(--bg); border-right: 1px solid var(--border); }}
  #diagrams .diagram {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 14px; margin-bottom: 18px; }}
  #diagrams h2 {{ margin: 0 0 8px; font-size: 14px; color: var(--accent); }}
  aside {{ overflow: auto; padding: 18px; background: var(--panel); }}
  aside h2 {{ margin: 0 0 4px; font-size: 16px; color: var(--accent); }}
  aside .step-class {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 10px; }}
  aside h3 {{ margin: 18px 0 6px; font-size: 13px; color: #333; border-bottom: 1px solid var(--border); padding-bottom: 4px; }}
  aside p {{ margin: 4px 0; line-height: 1.45; }}
  aside .docstring {{ white-space: pre-wrap; background: var(--code-bg); padding: 8px 10px; border-radius: 4px; font-size: 12.5px; color: #333; }}
  aside ul {{ padding-left: 18px; margin: 6px 0; }}
  aside li {{ margin: 3px 0; font-size: 13px; }}
  aside .kind-tag {{ display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 8px; margin-right: 6px; vertical-align: middle; font-weight: 600; }}
  aside .kind-next {{ background: #d6e4ff; color: #1d3a8a; }}
  aside .kind-menu {{ background: #ebe1ff; color: #5a2db5; }}
  aside .kind-form {{ background: #fff3d6; color: #8a5a1d; }}
  aside .kind-rerender {{ background: #e6e6e6; color: #555; }}
  aside .kind-guard {{ background: #f0e0e0; color: #802020; font-style: italic; }}
  aside .kind-success {{ background: #dcecdc; color: #1d4d1d; }}
  aside .kind-abort {{ background: #f6dada; color: #5d1d1d; }}
  aside .kind-raise {{ background: #fae0a0; color: #6a4a0a; }}
  aside code {{ background: var(--code-bg); padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  aside .empty {{ color: var(--muted); font-style: italic; font-size: 12.5px; }}
  aside .source {{ font-size: 11.5px; color: var(--muted); margin-bottom: 12px; }}
  aside .ui-block {{ background: var(--code-bg); padding: 8px 10px; border-radius: 4px; font-size: 12.5px; }}
  aside .ui-block .ui-title {{ font-weight: 600; margin-bottom: 4px; }}
  aside .ui-block pre {{ white-space: pre-wrap; margin: 0; font-family: inherit; }}
  .legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 8px; font-size: 12px; color: var(--muted); }}
  .legend .item {{ display: flex; align-items: center; gap: 5px; }}
  .legend .swatch {{ display: inline-block; width: 14px; height: 6px; border-radius: 2px; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
</head>
<body>
<header>
  <h1>Philips Sonicare BLE — Config Flow Map</h1>
  <span class="meta">{CFG_PATH.relative_to(REPO)} · click any step</span>
</header>
<main>
  <div id="diagrams">
    <div class="legend">
      <div class="item"><span class="swatch" style="background:#3a5fa8"></span>step</div>
      <div class="item"><span class="swatch" style="background:#3a8a3a"></span>create_entry</div>
      <div class="item"><span class="swatch" style="background:#a83a3a"></span>abort</div>
      <div class="item">─── next  · · · form/menu  ━━━ terminal</div>
    </div>
    {''.join(f'<div class="diagram"><h2>{d["label"]}</h2><div class="mermaid-target" id="diagram-{i}"></div></div>' for i, d in enumerate(diagrams))}
  </div>
  <aside id="details">
    <h2>Click a step to see details</h2>
    <p class="empty">Each box in the diagram corresponds to one <code>async_step_*</code> method. Click on it to see its source line range, docstring, transitions, error keys and matching strings.json entry.</p>
  </aside>
</main>
<script>
  mermaid.initialize({{ startOnLoad: false, securityLevel: 'loose', theme: 'default', flowchart: {{ htmlLabels: true, curve: 'basis' }} }});

  const DETAILS = {json.dumps(details_blob, ensure_ascii=False, indent=2)};
  const DIAGRAMS = {json.dumps(diagram_sources, ensure_ascii=False, indent=2)};

  async function renderAll() {{
    for (const d of DIAGRAMS) {{
      try {{
        // Programmatic render — the source comes straight from a JS string,
        // so newlines in the diagram body cannot be stripped by the
        // browser's whitespace-normalisation on inline text content.
        const {{ svg }} = await mermaid.render(`m-${{d.idx}}`, d.src);
        const target = document.getElementById(`diagram-${{d.idx}}`);
        target.innerHTML = svg;
        // Attach click handlers to each rendered SVG node.
        target.querySelectorAll('g.node').forEach(node => {{
          const raw = node.id || node.getAttribute('data-id') || '';
          const m = raw.match(/S(\\d+)_(.+?)(?:-\\d+)?$/);
          if (!m) return;
          const [, classIdx, stepName] = m;
          node.style.cursor = 'pointer';
          node.addEventListener('click', () => showDetails(classIdx, stepName));
        }});
      }} catch (err) {{
        const target = document.getElementById(`diagram-${{d.idx}}`);
        if (target) {{
          target.innerHTML = `<div style="color:#a83a3a"><b>Render failed for ${{d.label}}</b><pre>${{escape(err && err.message ? err.message : err)}}</pre></div>`;
        }}
        console.error(err);
      }}
    }}
  }}

  document.addEventListener('DOMContentLoaded', () => {{ renderAll(); }});

  function escape(s) {{
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }}

  function renderTransitions(transitions) {{
    if (!transitions || transitions.length === 0) {{
      return '<p class="empty">No transitions detected.</p>';
    }}
    const items = transitions.map(([kind, target]) => {{
      const label = target.startsWith('__SUCCESS__') ? 'create_entry'
                  : target.startsWith('__ABORT__:') ? `abort (${{target.split(':',2)[1]}})`
                  : target;
      return `<li><span class="kind-tag kind-${{kind}}">${{kind}}</span><code>${{escape(label)}}</code></li>`;
    }});
    return `<ul>${{items.join('')}}</ul>`;
  }}

  function renderErrors(errors, strings) {{
    if (!errors || errors.length === 0) return '<p class="empty">No errors set in this step.</p>';
    return `<ul>${{errors.map(e => {{
      const text = strings && strings[e] ? strings[e] : '<i>(no string entry)</i>';
      return `<li><code>${{escape(e)}}</code> — ${{escape(text)}}</li>`;
    }}).join('')}}</ul>`;
  }}

  function renderUi(d) {{
    const hasUi = d.ui_title || d.ui_description || (d.ui_data && Object.keys(d.ui_data).length);
    if (!hasUi) return '<p class="empty">No strings.json entry for this step.</p>';
    let html = '<div class="ui-block">';
    if (d.ui_title) html += `<div class="ui-title">${{escape(d.ui_title)}}</div>`;
    if (d.ui_description) html += `<pre>${{escape(d.ui_description)}}</pre>`;
    const fields = Object.entries(d.ui_data || {{}});
    if (fields.length) {{
      html += '<div style="margin-top:8px"><b>Fields:</b><ul>';
      for (const [key, lab] of fields) {{
        const desc = (d.ui_data_description || {{}})[key];
        html += `<li><code>${{escape(key)}}</code> — ${{escape(lab)}}${{desc ? ` <i>(${{escape(desc)}})</i>` : ''}}</li>`;
      }}
      html += '</ul></div>';
    }}
    html += '</div>';
    return html;
  }}

  function showDetails(idx, name) {{
    const d = DETAILS[`${{idx}}::${{name}}`];
    if (!d) return;
    const cfgPath = '{CFG_PATH.relative_to(REPO)}';
    const html = `
      <h2><code>async_step_${{escape(d.name)}}</code></h2>
      <div class="step-class">${{escape(d.class)}}</div>
      <div class="source">${{escape(cfgPath)}}:${{d.line_start}}–${{d.line_end}}</div>
      <h3>Docstring</h3>
      ${{d.docstring ? `<div class="docstring">${{escape(d.docstring)}}</div>` : '<p class="empty">No docstring.</p>'}}
      <h3>Transitions</h3>
      ${{renderTransitions(d.transitions)}}
      <h3>Error keys set</h3>
      ${{renderErrors(d.errors, d.error_strings)}}
      <h3>UI strings</h3>
      ${{renderUi(d)}}
    `;
    document.getElementById('details').innerHTML = html;
  }}
  // Expose globally so Mermaid's anchor handlers can reach us.
  window.showDetails = showDetails;
</script>
</body>
</html>
"""


def main() -> int:
    if not CFG_PATH.exists():
        print(f"error: {CFG_PATH} not found", file=sys.stderr)
        return 1
    flow = parse_flow(CFG_PATH.read_text())
    strings = load_strings()
    OUTPUT.write_text(emit_html(flow, strings))
    n = sum(len(c["steps"]) for c in flow["classes"])
    print(f"wrote {OUTPUT}  ({n} steps across {len(flow['classes'])} class(es))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
