---
name: wsg
description: Logga in som sig själv mot Microsoft 365 (Graph) och WeSports Arena med wsg-cli, och använd det för att skicka mail, svara på mail i tråden och arkivera trådar, skriva, läsa och söka i Teams, läsa Planner-tavlor, skapa engångslänkar för lösenord och nycklar, eller hämta en token till ett eget script. Use when the user wants to send a mail, answer a mail in its thread, archive a mail thread, or send a Teams message from the terminal, read a Teams chat or find something said in Teams, asks what is on a Planner board or what Planner tasks are theirs, needs to hand someone a password or API key safely, asks for a Graph or Arena token, or says "wsg". Exempel "skicka ett mail till Anna med rapporten", "svara Erik att torsdag funkar", "arkivera tråden", "skriv till Erik i Teams att det är klart", "vad skrev Gustav i Teams idag", "var var det någon nämnde returfrakten i Teams", "vad har jag i Planner", "vad ligger på tavlan Vartex Service", "skicka lösenordet till Erik på ett säkert sätt", "logga in wsg", "uppdatera wsg".
---

# wsg: inloggning mot Microsoft 365 och Arena

`wsg` är installerat på den här datorn. Kör det som `python -m wsg` (`python3 -m wsg`
på macOS). Det fungerar även när `wsg` inte finns i PATH. Läs `python -m wsg KOMMANDO
--help` när något är oklart.

Personen du hjälper är troligen inte tekniskt lagd. Kör kommandona själv och svara
med resultatet i vanlig svenska, inte med kommandoutskrifter.

## Inloggning

Allt körs som personen själv, med deras egna rättigheter. wsg har inga egna
nycklar och kan aldrig mer än personen kan i Outlook, Teams och Planner.

```
python -m wsg graph login        skriver ut en adress; personen öppnar den och loggar in
python -m wsg graph status       vem som är inloggad och vad inloggningen omfattar
```

- **Be aldrig om lösenord, kod eller token i chatten.** Inloggningen sker i
  personens egen webbläsare. Du ser bara adressen.
- `login` skriver ut adressen och väntar tills inloggningen är klar (högst tio
  minuter). Kör den i bakgrunden, ge personen adressen som text och säg att hen
  ska öppna den. Öppna aldrig webbläsaren själv.
- Inloggningen håller i 90 dagar efter senaste användning. Svarar ett kommando
  `Not signed in. Run: wsg graph login` (slutkod 2), logga in igen.

## Skicka mail

```
python -m wsg graph sendmail --to anna@example.com --subject "Ämne" --dry-run <<'EOF'
Hej Anna,

**Det viktiga:** texten är markdown, radbrytningar följer med.
EOF
```

- Texten är markdown och går ut formaterad, som i Teams: fetstil, listor, `kod`,
  kodblock, citat, tabeller och länkar, med stilarna inline så att Outlook visar
  dem. Formatera efter innehållet: fetstil på det som inte får missas, det
  mottagaren ska klistra in eller känna igen (ett kommando, ett fältnamn, JSON)
  som `kod` eller kodblock, steg som numrerad lista. Ett kort mail är bara stycken.
- `--plain` skickar texten som ren text, `--html` om texten redan är HTML.
  `wsg mail md` visar HTML:en, och `-o FIL` sparar den som en sida att öppna i
  webbläsaren.

- Kör alltid med `--dry-run` först och **visa hela mailet för personen** innan det
  skickas på riktigt. Det finns inget sätt att ta tillbaka ett skickat mail.
- `sendmail` skickar direkt, det blir inget utkast. En kopia hamnar i Skickat.
- `--attach FIL` för bilagor (upprepa för flera), högst cirka 3 MB totalt.
- `--cc` för kopia.
- `--send-at 2026-10-15T08:00` skickar senare, i lokal tid. Mailet väntar i Utkorgen
  och går även om datorn är avstängd. Ta bort det ur Utkorgen för att stoppa det.

## Svara på mail och arkivera tråden

```
python -m wsg mail reply MAIL-ID --dry-run <<'EOF'
Tack, torsdag funkar!
EOF
python -m wsg mail archive MAIL-ID --dry-run
```

- **Ett svar ska vara ett svar, inte ett nytt mail.** `sendmail` med "Re:" i ämnet
  hamnar utanför tråden. `mail reply` svarar i tråden och citerar originalet.
- MAIL-ID är mailets id, eller `mail:///messages/<id>` som M365-connectorn ger.
  Hitta mailet med connectorns sökning och svara sedan med wsg.
- `--all` svarar alla (avsändaren plus Till och Kopia, utom personen själv).
  `--attach FIL` för bilagor, högst cirka 3 MB.
- Texten är markdown, precis som i `sendmail`. `--plain` skickar den som den är.
- `--dry-run` visar mottagare, kopia och text. **Visa dem för personen** innan
  svaret går iväg.
- `--archive` flyttar sedan hela tråden från inkorgen till Arkiv. `mail archive`
  gör bara det, för en tråd som är klar utan svar. Ett flyttat mail får nytt id.

## Skriva i Teams

```
python -m wsg teams send --to erik@example.com --dry-run <<'EOF'
**Klart!** Så här gör du:
1. Öppna inställningarna
2. Klistra in `nyckeln`
EOF
```

- Texten är markdown och går ut formaterad: fetstil, listor, `kod`, kodblock,
  citat, tabeller och länkar. En radbrytning blir en radbrytning, och en lista
  direkt under en textrad blir en lista.
- `--to ADRESS` skriver i 1:1-chatten med personen (den hittas, eller skapas om den
  inte finns). `--chat ID` skriver i en bestämd chatt, t.ex. en gruppchatt.
- Texten tas från argumenten eller från stdin. Kör `--dry-run` först och **visa
  meddelandet för personen** innan det skickas. Ett skickat meddelande syns direkt
  hos mottagaren.
- Formatera efter innehållet: steg som numrerad lista, det mottagaren ska klistra in
  i ett kodblock, fetstil på det enda som inte får missas. Ett kort svar är en mening
  utan formatering.
- `wsg teams md` visar bara HTML:en för en text, utan att skicka något.

### Läsa och hitta i Teams

```
python -m wsg teams chats                     de senaste chattarna, med id
python -m wsg teams chats "Anna"              chattar vars namn eller deltagare matchar
python -m wsg teams read --name "Anna Berg"   en chatt, äldst först (-n 30 som standard)
python -m wsg teams read --to anna@example.com --since 2026-10-12
python -m wsg teams find "returfrakt"         meddelanden som nämner texten
```

- `read` väljer chatt med `--chat ID`, `--to ADRESS` (1:1-chatten) eller `--name TEXT`.
  Matchar namnet flera chattar listas de, välj då med `--chat`. Ett helt namn på en
  person ger 1:1-chatten med hen.
- `--images MAPP` sparar inklistrade bilder och skriver ut sökvägarna. **Titta på
  bilderna**: ofta står frågan i en skärmbild, inte i texten.
- `--files MAPP` sparar filer som delats i chatten (en Excel-fil, en PDF) och skriver
  ut sökvägarna. Filen ligger i avsändarens OneDrive, så det kräver en inloggning från
  0.9.0 eller senare. Står det 403 vid filen, kör `wsg graph login` en gång till.
- `find` söker i hela historiken, i chattar och kanaler, och visar var och vem.
  Sökordet kan vara KQL: `find "from:Anna faktura"`. Med chatt-id:t från en träff
  läser du sedan sammanhanget med `read --chat`.
- Säger `find` att sökningen inte gick har personen loggat in före 0.8.0. Den letar
  då bara i de senaste chattarna. `wsg graph login` en gång till ger full sökning.
  `--recent` gör samma lokala sökning med flit.
- Tider skrivs i lokal tid. `--json` ger råa tider, avsändarens id, text, bilder och
  filer, för script.
- För script: `--json` skriver ut chatId, id och tid, och `--sent-log FIL` lägger
  meddelandets id sist i en fil.

## Planner

Bara läsning. Inget här ändrar en uppgift.

```
python -m wsg planner mine                 personens egna uppgifter, närmast förfallodag först
python -m wsg planner plans                alla tavlor personen ser
python -m wsg planner tasks "Vartex"       en tavla; ett namnfragment räcker om det är unikt
python -m wsg planner task ID              en uppgift med beskrivning och checklista
python -m wsg planner link "Vartex" --bare adressen till en tavla
```

Saknas en tavla som personen vet finns är hen troligen inte medlem. Graph svarar då
med en tom lista, inte med ett fel.

## Lösenord och nycklar till någon annan

Ett lösenord eller en nyckel skickas aldrig som text i mail eller Teams. Gör en
engångslänk som förstörs när den öppnats och skicka länken i stället:

```
python -m wsg secret create --file nyckel.json --title "API-nyckel" --ttl 7d
```

- **Du ska aldrig se värdet.** Ligger det redan i en fil (en nyckelfil, en
  export), använd `--file`. Annars: be personen skapa länken själv på
  https://secrets.wesports.io och klistra in länken, inte värdet.
- Har personen klistrat in lösenordet i chatten ska det bytas. Gör inte en länk av
  det.
- stdout är bara länken. Utgångstid och kommandot för att förstöra den går till
  stderr.
- `--title` kan läsas av den som har länken innan den öppnats. Skriv vad det är,
  aldrig vad det innehåller.
- Ingen inloggning behövs.

## Arena

```
python -m wsg arena login      skriver ut en adress; personen loggar in i Arena och godkänner
python -m wsg arena whoami     vem som är inloggad
```

Samma regler som för Microsoft-inloggningen: kör `login` i bakgrunden, ge personen
adressen som text, öppna inte webbläsaren själv. Personen loggar in som vanligt i
Arena och trycker **Allow** på sidan som säger att ett kommandoradsverktyg vill
agera som hen. Inloggningen gäller tolv timmar, sedan frågar den igen.

Verktyget får göra exakt det personen får göra i Arena-appen, inget mer. Ett 403
betyder att personen saknar rätten i Arena, inte att något är fel i wsg.

## Token till ett eget script

```
python -m wsg graph token            en Graph-token, förnyas tyst
python -m wsg graph token --header   som "Bearer ..."
```

Skriv aldrig ut en token i chatten mer än nödvändigt, och lägg den aldrig i en fil.

## Admin-kommandon

`wsg user`, `device`, `intune` och `admin` finns bara hos IT, i ett separat paket.
Saknas de är det meningen.

## Uppdatera

```
python -m wsg update
```

Hämtar senaste versionen och skriver om den här skillen. Inloggningen ligger kvar.

## Felmeddelanden

| Meddelande | Betyder | Gör |
|---|---|---|
| `Not signed in. Run: wsg graph login` | ingen inloggning, eller en från en äldre version | logga in igen |
| `Ej beviljade scopes: ...` efter login | inloggningen saknar en behörighet | säg det till personen; IT behöver lägga till den i appen |
| HTTP 403 | personen saknar rätt till just det här | säg det rakt ut; det går inte att lösa härifrån |
| `AADSTS50105` eller "not assigned" | appen kräver tilldelning | hänvisa till IT |
