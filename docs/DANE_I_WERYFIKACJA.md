# Dane, architektura i weryfikacja

[Strona projektu](../README.md) · [Instalacja](INSTALACJA.md) · [Instrukcja obsługi](INSTRUKCJA.md)

## Jak powstaje raport

Kreator HTML, JavaScript i Sass działa w przeglądarce. API FastAPI pobiera granice działki i dane z niezależnych modułów. Reguły analizują pogodę, glebę, teren, hydrologię i satelitę, a następnie odnoszą bilans opadów i strat wody do wybranej uprawy. Filtr i ranking łączą wykryte problemy z działaniami z katalogu. Po wyborze działań aplikacja oblicza plan i pokazuje jego koszty, próg opłacalności oraz możliwe dofinansowania. Widok końcowy, eksport PDF i raport tekstowy korzystają z tego samego `plan_id`.

## Źródła i aktualizacja

| Dane | Zakres i sposób pobierania | Źródło i warunki korzystania |
| --- | --- | --- |
| Działka i adres | Bieżący obrys i identyfikator z ULDK lub WFS EGiB. Wyszukiwanie adresu przez UUG. Analiza historyczna używa współczesnych granic. | [GUGiK EGiB](https://www.geoportal.gov.pl/pl/dane/ewidencja-gruntow-i-budynkow-egib/), [usługa UUG](https://services.gugik.gov.pl/uug/) |
| Pogoda | Dobowe dane stacji klimatycznych. Kopia SQLite w repozytorium obejmuje okres od stycznia 2016 do września 2026. Import zakończono 21.11.2026, ostatnia obserwacja w kopii jest z 30.09.2026. Analiza uwzględnia obserwacje dostępne dla wybranej daty. Import odświeża skrypt `scripts/import_imgw.py`. | [IMGW-PIB, dane publiczne i regulamin](https://danepubliczne.imgw.pl/) |
| Gleba | SoilGrids v2.0 przez WCS. Własności gleby wykorzystane w modelu dotyczą warstwy 0–30 cm. Aplikacja odpytuje usługę lub używa zapisanej kopii wycinka dla działki. | [ISRIC, CC BY 4.0](https://isric.org/explore/soilgrids/) |
| Teren | GUGiK NMT przez WCS. Nachylenie, ukształtowanie i potencjalne drogi spływu. Pobieranie na żywo z pamięcią podręczną. | [GUGiK, dane do bezpłatnego pobrania](https://www.gov.pl/web/gugik/dane-udostepniane-bez-platnie-do-pobrania-z-serwisu-wwwgeoportalgovpl) |
| Hydrologia | Cieki i rowy z BDOT10k oraz obszary zagrożenia powodziowego z MZP/MRP. Usługi mapowe odpytywane dla działki. | [GUGiK BDOT10k](https://www.gov.pl/web/gugik/dane-udostepniane-bez-platnie-do-pobrania-z-serwisu-wwwgeoportalgovpl), [Wody Polskie, warunki ponownego wykorzystania](https://www.gov.pl/web/wody-polskie/udostepnianie-danych-z-systemu-informacyjnego-gospodarowania-wodami) |
| Satelita | Sentinel-2 L2A, wskaźniki NDVI i NDMI z obserwacji spełniających kryteria jakości. Sceny i metadane są dobierane do działki i daty analizy. | [Earth Search i Sentinel-2 COG](https://registry.opendata.aws/sentinel-2-l2a-cogs/), [warunki programu Copernicus](https://dataspace.copernicus.eu/terms-and-conditions) |
| Podkład mapy | Kafelki OpenStreetMap lub ortofotomapa Geoportalu ładowane przez przeglądarkę. | [OpenStreetMap, ODbL](https://www.openstreetmap.org/copyright), [zasady korzystania z kafelków](https://operations.osmfoundation.org/policies/tiles/), [Geoportal](https://www.geoportal.gov.pl/) |

Daty obserwacji zależą od wyników zapytania i są podawane w raporcie. Mapy referencyjne gleby, terenu i hydrologii mogą pochodzić z innego okresu niż analiza. Data pobrania i data obserwacji opisują różne zdarzenia. Tryb testowy korzysta z syntetycznych danych zamiast usług analitycznych.

## Modele, komponenty i AI

- Silnik używa jawnych reguł i katalogów. Obliczenia korzystają m.in. z uproszczenia Hargreavesa dla ET₀, współczynników upraw Kc oraz szacunków retencji z parametrów gleby. Progi diagnozy można sprawdzić w [`analysis_rules.json`](../backend/app/data/analysis_rules.json), a parametry upraw i reguły działań w [`backend/app/data`](../backend/app/data/). [Podstawa metodyczna FAO‑56](https://www.fao.org/4/x0490e/x0490e00.htm)
- [Zależności Pythona](../requirements.txt) obejmują FastAPI, Pydantic, pyproj i pytest (MIT), Uvicorn, Shapely, HTTPX, Rasterio i pypdf (BSD), fpdf2 (LGPL‑3.0), tzdata (Apache‑2.0) i NumPy (BSD oraz licencje części składowych). Szczegółowe licencje są w metadanych zainstalowanych pakietów.
- Mapa używa [Leaflet 1.9.4 (BSD‑2‑Clause)](https://github.com/Leaflet/Leaflet/blob/v1.9.4/LICENSE). Style kompiluje [Dart Sass (MIT)](https://github.com/sass/dart-sass/blob/main/LICENSE). Czcionka [Literata (SIL Open Font License)](../backend/app/static/fonts/OFL-Literata.txt) jest dołączona do projektu.
- OpenAI Codex wspierał prace nad kodem, testami i dokumentacją. W działającej aplikacji raport i rekomendacje powstają z danych i reguł bez wywoływania modelu generatywnego. [Warunki usług OpenAI](https://openai.com/policies/eu-terms-of-use/)

## Jak sprawdzić wynik

1. Przejdź od działki i daty do PDF-u według [instrukcji obsługi](INSTRUKCJA.md). Sprawdź, czy identyfikator, uprawa i data odpowiadają wejściu.
2. Sprawdź daty i dostępność źródeł. Porównaj uzasadnienie działań z diagnozą działki i zweryfikuj w terenie lokalne warunki spływu, drenaż i możliwości retencji. W przypadku nawadniania osobno potwierdź dostępność źródła wody i warunki jej poboru.
3. Przećwicz przypadek z brakami w trybie testowym na działce `149901_2.0001.303` z datą `15.06.2019`. Testy `pytest -q` sprawdzają m.in. ścieżkę do PDF-u, daty historyczne, dane częściowe i zgodność treści raportu z planem.

Uproszczony bilans klimatyczny i wskaźniki satelitarne nie są pomiarem wilgotności gleby na działce ani oficjalnym bilansem suszy IUNG. Stacja IMGW może być oddalona, chmury ograniczają dostępność scen, a model gleby może obejmować szerszy obszar niż działka. Koszty z katalogu są orientacyjne, a dostępność wsparcia wymaga sprawdzenia w źródłach programu.

## Ograniczenia techniczne i skalowanie

Prototyp ma budżet 28 sekund na analizę i może zwrócić raport częściowy przy opóźnieniu zewnętrznej usługi. Czas odpowiedzi zależy m.in. od dostępności źródeł i zawartości pamięci podręcznej. Przy użytkowaniu w większej skali potrzebne byłyby automatyczne aktualizacje archiwum, współdzielona pamięć podręczna, limity pytań do usług i monitoring wolnych analiz. Koszty infrastruktury zależałyby od liczby użytkowników, transferu i przestrzeni na dane oraz od wyboru dostawcy kafelków mapowych.
