from pathlib import Path

web = Path("rtse/web")
meta = (web / "meta.js").read_text(encoding="utf-8")

start = meta.index("/** Rarity is only a number from the game")
end = meta.index("// --- Effects")
meta = meta[:start] + '''/**
 * The game only gives a rarity number. Colour it in the familiar white / green / blue / purple /
 * orange / cyan bands (the exact number-to-name mapping is not confirmed, so no names are shown).
 */
export function rarityColor(rarity) {
  if (rarity === null || rarity === undefined) return "#6b6657";
  if (rarity <= 1) return "#e8e2d0";
  if (rarity <= 3) return "#4ad65c";
  if (rarity <= 5) return "#3d8bff";
  if (rarity <= 7) return "#b25cff";
  if (rarity <= 9) return "#ff9a1f";
  return "#32e0d2";
}

''' + meta[end:]
(web / "meta.js").write_text(meta, encoding="utf-8")

html = '''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RTSE - Real-Time Save Editor</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="app.css">
</head>
<body>
  <header class="topbar">
    <div class="brand"><b>RTSE</b><span>Real-Time Save Editor</span></div>
    <div class="spacer"></div>
    <div id="conn" class="pill"><i></i><span>Connecting...</span></div>
  </header>
  <main class="app">
    <aside class="sidebar">
      <div class="side-head"><h2>Loadout</h2></div>
      <div class="search">
        <svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="search" type="search" placeholder="Search gear  ( / )" autocomplete="off">
      </div>
      <div id="inventory" class="inventory"></div>
    </aside>
    <section id="workspace" class="workspace"></section>
  </main>
  <div id="picker-root"></div>
  <div id="toasts"></div>
  <script type="module" src="app.js"></script>
</body>
</html>
'''
(web / "index.html").write_text(html, encoding="utf-8")
print("ok")
