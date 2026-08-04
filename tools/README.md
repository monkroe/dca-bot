# tools

Offline, read-only skriptai. Nieko nerašo, jokių orderių nesiunčia.

## Cap taisyklės backtest

`STATUS.md` (robert-os-hub) cituoja šiuos skaičius kaip pagrindą **gyvam**
prekybos taisyklės pakeitimui -- „legacy rule skipped ~22% of all days over
220d with 62% of those skips below H90", „8 other pairs: 16-25% skipped,
63-95% cheap", „new rule skips 0 days over 220d and never a cheap one over
500d". Iki 2026-08-04 juos pagaminę skriptai gulėjo tik laikinajame kataloge.

Kanoninis dokumentas negali remtis įrodymu, kurio nėra nė viename repo: dingus
skriptams tų skaičių nebeatkartotum, tik pakartotum.

| Failas | Ką atsako |
|---|---|
| `cap_backtest.py` | Kaip 7D cap būtų elgęsis per paskutines 220 dienų: dabartinė riba prieš `mid > H7 * 1.25` |
| `cap_pairs.py` | Ta pati taisyklė (`H7 x 1.20` IR `price > H90`) kitose kripto porose, prieš legacy |
| `cap_addendum.py` | Dienos, kurias spec taisyklė realiai vetuotų, plius H90 override patikra |
| `scenario_jump.py` | Roberto scenarijus: savaitė ties viena kaina, šuolis, savaitė svyravimo -- diena po dienos pagal IŠSIŲSTĄ taisyklę |

Paleidimas iš bet kur:

```
python3 tools/cap_backtest.py [dienos]
```

Kelias į `src/` išvedamas iš paties failo vietos. Absoliutūs keliai čia buvo
tol, kol failai gyveno laikinajame kataloge -- ta pati klaida, kurią `boot.sh`
išmoko brangiai: kiekvienas `~/` kelias jame buvo negyvas nuo tos dienos, kai
repo persikėlė, o skriptas apie tai pranešdavo tylėdamas.
