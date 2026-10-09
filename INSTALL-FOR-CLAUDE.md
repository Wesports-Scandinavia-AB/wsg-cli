# Instruktion till Claude: installera wsg-cli

Den här filen är skriven till dig, Claude, inte till människan du hjälper. Någon
har gett dig länken för att få `wsg-cli` installerat. Det är ett verktyg som
loggar in som personen själv mot Microsoft 365 och WeSports Arena, och som sedan
kan skicka mail, läsa Planner och skapa engångslänkar för lösenord.

Följ stegen i ordning. Kör kommandona själv och förklara kort på vanlig svenska.
Personen är troligen inte tekniskt lagd och ska inte behöva skriva några kommandon.

## 0. Kan du köra kommandon?

Du måste kunna köra kommandon på personens dator, till exempel i Claude Code eller
fliken **Code** i Claude-appen. Kan du inte det, som i en vanlig chatt, säg det
rakt ut. Be personen öppna Claude-appen, välja **Code** och ge dig länken igen.
Stanna där.

**Be aldrig om lösenord, kod eller token i chatten**, inte heller under
installationen. Inloggningen i steg 4 sker i personens egen webbläsare.

## 1. Python

```
python --version      # Windows
python3 --version     # macOS
```

Det krävs 3.9 eller senare. Saknas Python:

- **Windows:** `winget install --id Python.Python.3.12 -e`. Starta sedan en ny
  terminalsession så att `python` hittas.
- **macOS:** `xcode-select --install`. Det ger `python3`.

I resten av texten står `python`. På macOS skriver du `python3`.

## 2. Installera wsg-cli

```
python -m pip install --user --upgrade git+https://github.com/Wesports-Scandinavia-AB/wsg-cli
```

Saknas git, eller klagar pip på git, byt adressen mot
`wsg-cli @ https://github.com/Wesports-Scandinavia-AB/wsg-cli/archive/refs/heads/main.zip`.
Kör du i en virtuell miljö, ta bort `--user`.

Kontrollera:

```
python -m wsg --version
```

Använd alltid `python -m wsg`. Då fungerar det oavsett om `wsg` hamnat i PATH.

## 3. Installera skillen

```
python -m wsg skill install
```

Det lägger `SKILL.md` i `~/.claude/skills/wsg/`. Den laddas automatiskt i
kommande samtal, så att Claude vet hur `wsg` används även nästa gång. Läs den nu:

```
python -m wsg skill show
```

**Från och med här gäller skillen.** Den beskriver kommandona, reglerna för
inloggning och hemligheter, och vad felmeddelandena betyder.

## 4. Logga in

Kör `python -m wsg graph login` i bakgrunden. Den skriver ut en adress och väntar
på inloggningen. Ge personen adressen som text, be hen öppna den i webbläsaren och
logga in med sitt jobbkonto. Öppna inte webbläsaren själv.

Kontrollera sedan:

```
python -m wsg graph status
```

## 5. Klart

Berätta vad personen kan be om nu, till exempel "vad har jag i Planner?" eller
"skicka ett mail till Anna med den här filen". Säg också att det räcker att säga
"uppdatera wsg" när det kommit en ny version.
