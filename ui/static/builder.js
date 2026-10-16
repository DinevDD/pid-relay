// Rule helper for the edit page: turns a path template + content type +
// target into RewriteCond/RewriteRule lines and a matching test.
(function () {
  const $ = (id) => document.getElementById(id);
  const ta = (name) => document.querySelector(`textarea[data-name="${name}"]`);
  const typeSel = $("b-type"), custom = $("b-type-custom");
  if (!typeSel) return;
  typeSel.addEventListener("change", () => { custom.hidden = typeSel.value !== "custom"; });

  const reEsc = (s) => s.replace(/[.^$*+?()[\]{}|\\]/g, "\\$&");

  function compile(template) {
    template = template.trim().replace(/^\/+/, "");
    if (/\s/.test(template)) throw new Error("the path may not contain spaces");
    const vars = [];
    let regex = "", sample = "", last = 0;
    const tok = /\{([A-Za-z_][A-Za-z0-9_]*)(\*?)\}/g;
    let m;
    while ((m = tok.exec(template))) {
      const lit = template.slice(last, m.index);
      regex += reEsc(lit); sample += lit;
      if (vars.includes(m[1])) throw new Error(`variable {${m[1]}} used twice`);
      vars.push(m[1]);
      regex += m[2] ? "(.*)" : "([^/]+)";
      sample += m[2] ? "example/part" : "example";
      last = tok.lastIndex;
    }
    const rest = template.slice(last);
    if (/[{}]/.test(rest)) throw new Error("unbalanced { } in the path");
    regex += reEsc(rest); sample += rest;
    return { regex: "^" + regex + "$", vars, sample };
  }

  function fillTarget(target, vars, mode) {
    return target.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\*?\}/g, (_, v) => {
      const i = vars.indexOf(v);
      if (i < 0) throw new Error(`target uses {${v}}, which is not in the path`);
      return mode === "apache" ? "$" + (i + 1) : (v && "SAMPLE" + i);
    });
  }

  function build() {
    const path = $("b-path").value, target = $("b-target").value.trim();
    const type = typeSel.value === "custom" ? custom.value.trim() : typeSel.value;
    const status = $("b-status").value;
    if (!/^https?:\/\/\S+$/.test(target)) throw new Error("the target must be an absolute http(s) URL without spaces");
    const c = compile(path);
    if (c.vars.length > 9) throw new Error("at most 9 variables");
    const sub = fillTarget(target, c.vars, "apache");
    // sample values for the test
    const samples = c.vars.map((v, i) => (path.includes(`{${v}*}`) ? "example/part" : "example"));
    let expected = target.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\*?\}/g, (_, v) => samples[c.vars.indexOf(v)]);
    const lines = [`# ${path.trim() || "(namespace root)"}${type ? " as " + type : ""}`];
    if (type) lines.push(`RewriteCond %{HTTP_ACCEPT} ${reEsc(type)}`);
    lines.push(`RewriteRule ${c.regex} ${sub} [R=${status},L]`);
    const test = ["  - path: " + JSON.stringify(c.sample)];
    if (type) test.push("    accept: " + JSON.stringify(type));
    test.push("    status: " + status, "    location: " + JSON.stringify(expected));
    return { lines, test, regex: c.regex, typed: !!type };
  }

  function insertRule(text, r) {
    let lines = text.replace(/\s+$/, "").split("\n");
    if (text.trim() === "") lines = [];
    if (!lines.some((l) => /^\s*RewriteEngine\s+on\b/i.test(l))) lines.unshift("RewriteEngine on");
    const block = ["", ...r.lines];
    if (r.typed) {
      // place before the first generic rule with the same pattern
      const ruleRe = new RegExp("^\\s*RewriteRule\\s+" + r.regex.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s");
      for (let i = 0; i < lines.length; i++) {
        if (ruleRe.test(lines[i]) && !/^\s*RewriteCond/i.test(lines[i - 1] || "")) {
          let at = i;
          while (at > 0 && /^\s*#/.test(lines[at - 1])) at--;
          lines.splice(at, 0, ...r.lines, "");
          return lines.join("\n") + "\n";
        }
      }
    }
    return lines.concat(block).join("\n") + "\n";
  }

  function insertTest(text, t) {
    let s = text.replace(/\s+$/, "");
    if (!/^tests:\s*$/m.test(s)) s = (s ? s + "\n" : "") + "tests:";
    return s + "\n" + t.join("\n") + "\n";
  }

  const preview = $("b-preview");
  function show() {
    try { const r = build(); preview.textContent = r.lines.join("\n") + "\n\n# test\n" + r.test.join("\n"); preview.hidden = false; preview.classList.remove("err"); }
    catch (e) { preview.textContent = e.message; preview.hidden = !$("b-target").value; preview.classList.add("err"); }
  }
  ["b-path", "b-target", "b-type", "b-type-custom", "b-status"].forEach((id) => $(id).addEventListener("input", show));

  $("b-add").addEventListener("click", () => {
    let r;
    try { r = build(); } catch (e) { alertBox(e.message); return; }
    ta(".htaccess").value = insertRule(ta(".htaccess").value, r);
    ta("tests.yaml").value = insertTest(ta("tests.yaml").value, r.test);
    alertBox("Added. Adjust the test's sample values if needed, then Validate.", true);
  });

  function alertBox(msg, ok) {
    preview.hidden = false; preview.textContent = msg; preview.classList.toggle("err", !ok);
  }
})();
