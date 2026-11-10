# HomeMind 0.14.240 TEST — discovery przy nieczytelnej historii

Rozpakuj ZIP do osobnego katalogu pod `/addons/`. Plik `config.yaml` powinien leżeć w katalogu dodatku. Odśwież sklep, zainstaluj lokalny dodatek i przed uruchomieniem sprawdź wersję 0.14.240. Do walidacji użyj odizolowanej instalacji z dostępem do historii, na której wystąpił błąd.

- `COMPLETE` oznacza udane odczyty wszystkich zaplanowanych przedziałów. `PARTIAL` oznacza zakończenie klasyfikacji z zapisanymi lukami. Procent w `history.discovery_recorder.coverage_pct` mierzy pokrycie przedziałów zaplanowanych w danym skanie. Wskaźnik nie mierzy liczby stanów.
- `SKIPPED_HTTP500` oznacza potwierdzony 500 na minimalnym przedziale jednej encji. Po wyczerpaniu limitu błędów encji większy przedział z 500 jest `UNRESOLVED_HTTP500_BUDGET`, a pozostałe przedziały tej encji bez próby są `UNPROBED_HTTP500_BUDGET`. Żaden z nich nie zasila udanego pokrycia. Odczyty innych encji są kontynuowane w granicach limitów całego skanu.
- `BLOCKED` wskazuje, że discovery nie może potwierdzić wiarygodnego wyniku. `RECORDER_UNAVAILABLE` oznacza brak poprawnego wyniku niezależnych odczytów kontrolnych. Logi Core i Supervisora pozwalają rozstrzygnąć przyczynę HTTP 500.
- Discovery nie wyprowadza zmian stanu przez luki. Trening wymagający luki w historii celu lub potrzebnego wejścia zatrzymuje się przed odtwarzaniem danych i zapisuje powód przy agencie. Po przywróceniu odczytu użyj **Rescan**, aby zweryfikować luki ponownie, a następnie wznów trening.
- Zapisz status discovery, zdarzenia `discovery_recorder_controls` i `discovery_recorder_slice_skipped` oraz tracebacki HA Core dla odtworzonych HTTP 500. Zanonimizuj sekrety i dane prywatne przed udostępnieniem logów.
