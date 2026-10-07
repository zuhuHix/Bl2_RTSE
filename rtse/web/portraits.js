// The game's own character pictures (the ones from the character select screen), one per vault hunter.

export const CLASS_INFO = {
  assassin: { name: "Zer0", job: "Assassin" },
  siren: { name: "Maya", job: "Siren" },
  soldier: { name: "Axton", job: "Commando" },
  mercenary: { name: "Salvador", job: "Gunzerker" },
  mechromancer: { name: "Gaige", job: "Mechromancer" },
  psycho: { name: "Krieg", job: "Psycho" },
};

export function portraitUrl(key) {
  return `portraits/${key}.webp`;
}

/** Which class a class mod (or its name) belongs to. */
export function classFromText(text) {
  const t = String(text || "").toLowerCase();
  const rules = [
    [/assassin|zer0|zero/, "assassin"], [/siren|maya/, "siren"], [/soldier|commando|axton/, "soldier"],
    [/mercenary|gunzerker|salvador/, "mercenary"], [/mechromancer|gaige/, "mechromancer"], [/psycho|krieg/, "psycho"],
  ];
  const hit = rules.find(([re]) => re.test(t));
  return hit ? hit[1] : null;
}
