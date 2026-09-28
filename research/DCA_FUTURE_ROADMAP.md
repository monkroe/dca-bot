# DCA FUTURE ROADMAP

**Projektas:** `monkroe/dca-bot`  
**Paskirtis:** vidinis, nuolat pildomas research ir sprendimų registras  
**OWNER:** Roberto  
**Pradėta:** 2026-09-28  
**Būsena:** RESEARCH ONLY. Ne produkcinės implementacijos autorizacija.

## 1. Dokumento ribos

- Čia fiksuojame konkrečius pastebėjimus, hipotezes, vertinimo metodiką, eksperimentų rezultatus ir OWNER sprendimus.
- Šis failas nepakeičia esamų ADR, HANDOFF, CONTEXT, BOOT, SHUTDOWN ar produkcinės dokumentacijos. Vien dėl naujo įrašo jų neredaguojame.
- Veikiantis Kraken / Strike DCA, DCA botas, GitHub Actions ir jų duomenys lieka nepakeisti, kol OWNER atskirai neautorizuoja konkretaus darbo.
- Research nėra pavedimų vykdymo instrukcija. Jokių live orderių, Supabase writes, secrets, deploy, commit ar push be atskiros autorizacijos.
- Nepaversti pasiūlymo, agento teiginio ar istorinio roadmap įgyvendintu faktu. Naudoti `VERIFIED`, `SUPPLIED`, `HYPOTHESIS`, `OWNER DECISION`, `PROPOSED CHANGE`.
- Vieno kito žingsnio taisyklė. Naujas darbas tik tada, kai aišku, kokį sprendimą jis informuoja ir kodėl esamų įrodymų nepakanka.

## 2. Šaltiniai ir dabartinė atskaitos būsena

| Šaltinis | Statusas | Naudojimas |
|---|---|---|
| `DCA_Roadmap_v2.3.md` (2026-02-27) | Istorinis planas | Tikslai, kainų apibrėžimai ir ankstesnės hipotezės. Ne laikyti dabartinio diegimo įrodymu. |
| `src/kraken_run.py` | VERIFIED GitHub `main` 2026-09-28 peržiūros metu | Kode yra `exec_7d` ir `ohlc_h7` cap režimai; numatytosios reikšmės `exec_7d`, `0.03`, H90 guard išjungtas. **Faktinė live DB konfigūracija čia nepatikrinta.** |
| `tools/README.md`, `tools/cap_backtest.py`, `tools/cap_pairs.py`, `tools/cap_addendum.py`, `tools/scenario_jump.py` | VERIFIED, failai yra repo | Jau esanti read-only cap analizės bazė. Prieš siūlant naują skriptą tikrinti, ar klausimas neišspręstas esamais. |
| 2026-09-28 DCA Telegram pranešimas | SUPPLIED by OWNER | Konkretus tyrimo atvejis, ne pakankamas vieno pirkimo strategijos vertinimui. |

**Svarbi riba:** H7/H30/H90 yra Kraken daily-close SMA standartas. `exec_7d` yra kitas atskaitos dydis, skaičiuojamas iš vykdymų mid. Jų nesuplakti. Faktinė aktyvi `cap_mode` reikšmė turi būti pagrįsta DB ar įvykdymo telemetrija, kai jos tikrinimas bus reikalingas ir autorizuotas.

### 2026-09-28 atvejis

`KAS/USD FILLED` 07:02:03 CDT, `210.48519 KAS`, `price $0.04732`, `cost $9.9602`, `fee $0.0398`, `total $10.0000`; `mid $0.04723`, `impact +19.1 bps`, `all-in +59.1 bps`; `H7 $0.04348`, `H30 $0.03600`, `H90 $0.03063`.

**Klausimas:** ar tuo metu taikytas cap pakankamai atskyrė tęstinį kainos režimą nuo trumpalaikio šuolio, nepraleisdamas reikalingo KAS kaupimo? Vien šis pirkimas neįrodo blogo sprendimo. Reikia faktinės cap telemetrijos ir sąžiningo istorinio palyginimo.

## 3. Research backlog

| ID | Tema / tikrintinas klausimas | Būsena |
|---|---|---|
| DCA-R01 | Smart Cap: kaip elgiasi esamas `exec_7d` ir `ohlc_h7` skirtingose kainos trajektorijose? | OPEN |
| DCA-R02 | Režimo vertinimas: ar H7/H30/H90 kombinacija prideda vertės prieš paprastesnes taisykles? | HYPOTHESIS |
| DCA-R03 | Dynamic DCA: kiekio keitimas pagal kainos režimą, nepažeidžiant savaitinio biudžeto ir ilgalaikio kaupimo. | HYPOTHESIS |
| DCA-R04 | Carryover ir kapitalo rezervas: kada praleisti pinigai panaudojami, kokios ribos ir neigiamas poveikis? | HYPOTHESIS |
| DCA-R05 | Execution: maker/taker, spread, impact, all-in kaina, partial fills ir realios sąnaudos. | OPEN |
| DCA-R06 | Sistemos vientisumas: DCA ir Recycling bendros Kraken sąskaitos likvidumas, rezervai, API limitai ir operacinė atsakomybė. | FUTURE |

### R01. Smart Cap v2

- Dokumentuoti realiai aktyvų režimą, slenkstį, H90 guard, skip reason ir duomenų praradimo / unavailable-reference semantiką. Nespėti pagal komentarus kode.
- Atskirti faktinę rinkos kainos atskaitą (`mid`) ir kiekvieną cap reference (`exec_7d` arba H7).
- Išsiaiškinti, ar kylantis trumpasis vidurkis paskui kainą palieka nuolatinio režimo pakilimo akląją zoną.
- Įvertinti ir priešingą riziką: po kritimo atšokimą palaikyti „brangiu“ bei praleisti kaupimą, nors kaina tebėra žemiau ilgalaikės atskaitos.
- Prieš bet kokį keitimą palyginti su paprastu periodiniu pirkimu ir esama produkcine taisykle.

### R02–R04. Sprendimo ir biudžeto hipotezės

- Iš anksto fiksuoti hipotezes, intervalus, duomenų prieinamumo momentą, laikotarpius ir palyginimo bazę. Parametrų neoptimizuoti pagal tą patį vertinamą laikotarpį.
- Kandidatai yra tyrimo objektai, o ne priimtos taisyklės: H7/H30/H90 režimas, kintamas pirkimo dydis, bounded skip, carryover.
- Apskaityti nepanaudotus USD / USDC, vėliau įvykdytus pirkimus, biudžeto ribų laikymąsi ir kainos kilimo metu praleistą KAS kiekį.
- Vykdymo apribojimai, finansavimo laikas ir sąskaitos likvidumas yra modeliavimo sąlygos, ne nemokamas kapitalas.

### R05. Execution quality

- Lyginti sprendimo momento mid su tikra fill kaina, mokesčiais ir galutine gauta KAS suma.
- Tikrinti maker fill rate, fallback, partial fills, re-peg/cancel kaštus, slippage ir API rate budget.
- Kraken ir Strike rezultatus lyginti tik su aiškia vienoda all-in metrika ir atskirais venue apribojimais.

### R06. Sąveika su Recycling

- DCA ir Recycling lieka atskiros strategijos. Jų išlaidų, rezultatų ir pavedimų istorijos nesumaišyti.
- Jeigu jos dalijasi Kraken sąskaita, reikia vienareikšmio laisvo balanso bei orderių rezervų autoriteto ir reconciliation.
- Benas teikia pranešimus bei autorizuotas komandas; prekybos variklis neturi priklausyti nuo Telegram. Robert OS išlieka OS-first.
- Šiame dokumente nesprendžiame Recycling frozen engine ar live integracijos. Tam galioja atskiras jo kontraktas ir OWNER sprendimai.

## 4. Vertinimo metodas ir priėmimo kriterijai

1. Prieš testą užfiksuoti hipotezę, kandidato parametrus, baseline, duomenų kilmę, istorijos intervalą ir laiko žymų semantiką.
2. Atskiri baseline: (a) paprastas periodinis pirkimas tuo pačiu biudžetu, (b) faktinė esama DCA politika, (c) aiškiai aprašytas kandidatas. Visi naudoja vienodas finansavimo ir fee prielaidas.
3. Nenaudoti būsimų OHLC close ar būsimos sandorio informacijos ankstesniam sprendimui. Atskirti rinkos duomenų ir jų realaus prieinamumo laiką.
4. Matuoti: nupirktą KAS, bendrą panaudotą kapitalą, likusį cash/carryover, all-in KAS kainą, praleistus pirkimus, vėliau atgautą / neatgautą ekspoziciją, vykdymo sąnaudas ir nesuveikusius pavedimus.
5. Rezultatus skaidyti pagal rinkos režimus ir atskirą nevertintą laikotarpį; nenurodyti tik pelningiausio pavyzdžio.
6. Atskirti simuliaciją, dry-run, kontrolinį pilotą ir live rezultatą. Joks backtest savaime nesuteikia live autorizacijos.
7. Jei duomenys, finansavimo / pavedimų modelis ar prieinamumo laikas nepakankami, rezultatas `INSUFFICIENT EVIDENCE`, ne „PASS“.

## 5. Įrašų ir OWNER sprendimų registras

Tolimesnius įrašus papildyti čia, ne ADR ar handoff. Viena eilutė ar trumpas blokas, kai yra naujas įrodymas arba sprendimas.

| Data | ID | Klasė | Įrašas | Pasekmė / next step |
|---|---|---|---|---|
| 2026-09-28 | DCA-R00 | OWNER DECISION | Sukurti atskirą gyvą DCA research roadmap, neapkraunant ADR / HANDOFF / CONTEXT / BOOT / SHUTDOWN. | Research dokumentas atskiras nuo produkcinio kodo. |
| 2026-09-28 | DCA-R01 | SUPPLIED | Užfiksuotas 07:02:03 CDT KAS pirkimas ir H7/H30/H90 rodikliai. | Kandidatas vėlesnei cap analizei, ne strategijos FAIL. |
| 2026-09-28 | DCA-R01 | VERIFIED CODE | Repo yra du cap režimai ir esami cap tyrimo skriptai. | Prieš naują diagnostiką naudoti turimus šaltinius. Live DB parametrai nepatikrinti. |

### Naujo įrašo šablonas

`YYYY-MM-DD | DCA-Rxx | VERIFIED / SUPPLIED / HYPOTHESIS / OWNER DECISION / PROPOSED CHANGE | Faktas arba pasiūlymas + šaltinis / commit / duomenų intervalas | Poveikis / vienas next step`

**Redagavimo taisyklė:** ištaisant ankstesnę išvadą išsaugoti jos kilmę ir pažymėti `SUPERSEDED`, o ne tyliai ištrinti istoriją. Pasikeitus produkcinei būsenai atnaujinti §2 tik su nauju įrodymu. Nei vienas šio failo papildymas pats savaime neleidžia keisti kodo ar paleisti prekybos.
