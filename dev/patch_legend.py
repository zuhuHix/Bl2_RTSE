from pathlib import Path

js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")
old = 'h("ol", { class: "legend" }, detail.slots.map((slot, index) => legendRow(slot, index, detail))))),'
new = 'detail.slots.length > 0 && h("ol", { class: "legend" }, detail.slots.map((slot, index) => legendRow(slot, index, detail))))),'
assert s.count(old) == 1
js.write_text(s.replace(old, new), encoding="utf-8")
print("patched")
