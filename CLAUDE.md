# wsg-cli

Python-CLI och bibliotek som loggar in som personen själv mot Microsoft Graph och
WeSports Arena, och gör mail, Planner, engångslänkar och Arena åtkomligt från
terminalen eller Claude. `src/`-layout, bara standardbiblioteket, argparse, entry
point `wsg`. IT-kommandona (`wsg user`, `device`, `intune`, `admin`, ...) ligger i
det privata `wsg-admin-cli` och kopplas in via entry point `wsg.commands`.

## Regler

- **Varje ändring i wsg uppdaterar Tabula i samma arbete.** wsg-posten i
  `code/wesports/tabula/projects.json` (`id: wsg-cli`) säljer verktyget till
  kollegor: `highlights`, `examples` och `getStarted.features` ska alltid
  beskriva det som finns i dag, inget mer och inget mindre. Nytt kommando,
  ändrat beteende eller borttagen funktion: rätta posten, kör Tabulas gate
  (`pnpm lint && pnpm typecheck && pnpm test`), committa och pusha där, och
  **deploya för hand** (`pnpm run deploy` med Cloudflare-token, se Tabulas
  README). En push till Tabulas `main` deployar inte. Tabulas text är engelsk
  och säljande, men varje påstående måste vara sant.
- **Repot är publikt.** Inga interna värdnamn, IP-adresser, resursgrupper,
  subscription-id, delade brevlådor eller kollegors namn och adresser, inte i
  kod, kommentarer, README eller commit-meddelanden. Sådant hör hemma i
  `wsg-admin-cli`. Historiken skrevs om 2026-10-09 för att få bort det; den gamla
  ligger i det privata `wsg-cli-old` och får aldrig pushas hit.
- **Scopes ska stämma med appen.** `src/wsg/scopes.py` är exakt det som Entra-appen
  "WeSports CLI" (6a691e2e-…) har admin-medgivande för. Ett nytt scope kräver
  både raden här och en ändring av appens API-behörigheter plus nytt medgivande
  i tenanten. Annars syns det bara som "Ej beviljade scopes" efter login.
  Graph kräver ofta ett läs-scope bredvid skrivscopet.
- **Basen är det alla får.** Katalog-, enhets- och policyskrivningar hör inte hit
  utan i `wsg-admin-cli`, som loggar in via en egen app med tilldelning.
- Inga tredjepartsberoenden. Inga hemligheter i repot eller i argv; ett hemligt
  värde läses från stdin eller fil.
- Språk: dokumentation och utskrifter till användaren på svenska, utom README
  (engelska). Kod, kommentarer och `--help` på engelska.

## Struktur

- `src/wsg/auth.py`: Graph-inloggning, token-cache och refresh. `Client` per
  Entra-app, så att wsg-admin-cli kan använda samma kod mot sin app.
- `src/wsg/arena.py`: Arena-inloggning via Arenas OAuth. Använder den publika
  klienten `cl_wsg_cli` (ingen hemlighet), om inte `arena.json` anger en egen.
- `src/wsg/mscal.py`, `planner.py`, `onetime.py`: kalenderspegling, Planner
  (bara läsning) och engångslänkar på secrets.wesports.io.
- `src/wsg/cli.py`: argparse-kommandona, plus `skill` och `update`.
- `src/wsg/skill/SKILL.md`: skillen som `wsg skill install` och `wsg update` lägger
  i `~/.claude/skills/wsg/`. Den är den enda beskrivningen av hur en assistent
  använder wsg. Ändras ett kommando, ändra skillen i samma commit.
- `INSTALL-FOR-CLAUDE.md`: bara installationen, sedan pekar den på skillen.

Versionen står bara i `src/wsg/__init__.py`. Höj den vid varje ändring som
användare märker, så att `wsg update` visar att något hänt.

Commit direkt på `main`, en commit per sak som kan backas för sig.
