<p align="center">
  <img src="backend/app/static/logo-hydroplon.svg" width="110" alt="Logo HydroPlon">
</p>

<h1 align="center">HydroPlon</h1>

<p align="center">Diagnoza niedoboru wody i plan działań dla działki rolnej</p>

<p align="center">
  <a href="docs/INSTALACJA.md"><strong>Instalacja</strong></a> ·
  <a href="docs/INSTRUKCJA.md"><strong>Instrukcja obsługi</strong></a> ·
  <a href="docs/assets/hydroplon-przykladowy-raport.pdf"><strong>Przykładowy raport PDF</strong></a> ·
  <a href="docs/DANE_I_WERYFIKACJA.md"><strong>Dane i weryfikacja</strong></a>
</p>

## Uruchom aplikację

Na Windows pobierz całe repozytorium i otwórz [`Uruchom HydroPlon.cmd`](Uruchom%20HydroPlon.cmd). Potrzebujesz Pythona 3.13. Skrypt instaluje zależności i otwiera kreator na [127.0.0.1:8000/app](http://127.0.0.1:8000/app). Polecenia dla macOS i Linux oraz uruchomienie na danych testowych znajdziesz w [instrukcji instalacji](docs/INSTALACJA.md).

## Od działki do raportu

1. Wyszukaj działkę po adresie lub identyfikatorze albo wskaż ją na mapie. Wybierz datę analizy.
2. Wybierz uprawę i działania, które chcesz uwzględnić w planie.
3. Przeczytaj diagnozę, uzasadnienie zaleceń, kosztorys i plan wdrożenia. Pobierz PDF w tym samym układzie.

Raport pokazuje stan niedoboru wody i jego możliwe przyczyny, zalecenia retencyjne i agrotechniczne oraz opcjonalne nawadnianie. Podaje też źródła, daty obserwacji IMGW i Sentinel‑2, niepewność oraz braki danych. Wyniki są orientacyjne.

<details>
<summary>Zobacz kreator</summary>

![Wybór jednej z ośmiu upraw w aplikacji HydroPlon](docs/assets/hydroplon-wybor-uprawy.webp)

</details>

## Gdzie co jest

| Katalog | Zawartość |
| --- | --- |
| [`backend/`](backend/) | API, dane, obliczenia, raporty i zależności Pythona |
| [`frontend/`](frontend/) | Style kreatora oraz konfiguracja i zależności Sass |
| [`docs/`](docs/) | Instrukcje, opis źródeł i przykładowy PDF |
| [`scripts/`](scripts/) | Uruchamianie, import danych i narzędzia pomocnicze |
| [`tests/`](tests/) | Testy obliczeń, API i eksportu raportu |
