# Audyt ręcznej nauki — 0.11.0

## Wniosek

W 0.10.9 ręczne uczenie zapisywało prawdziwe aktualizacje modelu, ale nie gwarantowało skutecznej korekty decyzji, nie oferowało selektywnego cofania i wymagało bieżącego kontekstu. W 0.11.0 poprawiono te trzy elementy. Skuteczność na rzeczywistych domowych preferencjach wymaga oceny na danych użytkownika; testy dowodzą zachowania mechanizmu, nie deklarowanej procentowej jakości komfortu.

## Ustalenia i zmiany

1. **Słaba pojedyncza korekta.** DiagonalLinUCB kumuluje statystyki A i b. Po tysiącach spójnych przykładów pojedyncze odrzucenie starej akcji i dodanie nowej może nie zmienić zwycięzcy. Test odtwarza to dla 2000 próbek. Zwiększanie nagrody bez ograniczeń mogłoby rozregulować inne konteksty. Nowy dziennik etykiet ma pierwszeństwo w bliskim, zgodnym kontekście; bazowe macierze pozostają niezależne.
2. **Cofanie przez kopię modelu byłoby błędne.** Nadpisywałoby naukę powstałą po kliknięciu. Undo dezaktywuje jedną etykietę i ponownie oblicza Desired. Wycofana instrukcja nie może przejść przez Executor jako wcześniej utworzony intent. Nowa etykieta unieważnia również starszą zakolejkowaną decyzję modelu bazowego, zanim trafi do urządzenia. Oczekujące efekty akcji z etykiety nie dopisują nagród do bazowego RL.
3. **Historyczna etykieta potrzebuje historycznego kontekstu.** Punkt i wszystkie wymagane opóźnienia są odczytywane przez indeks entity_id,ts z warunkiem ts <= wybrany czas. Bieżące stany oraz dzisiejsza prognoza obecności nie zastępują braków. Wykres rekonstruuje aktualną politykę, co jest jawnie opisane.
4. **Pewność modelu nie jest siłą instrukcji użytkownika.** Nie ustawiamy sztucznego 100% po kliknięciu. Control może wykonać aktywną dopasowaną etykietę mimo niskiej statystycznej pewności modelu bazowego. Nadal wymaga kwalifikacji i przeglądu Control, poprawnego stanu, kontekstu, zakresów i normalnego wykonania przez Executor.
5. **Trening historyczny i ręczne poprawki nie powinny nadpisywać się.** Etykiety są oddzielne od macierzy i przeżywają restart oraz przebudowę zgodnego modelu. Zmiana schematu cech lub zakresu nie dopasowuje na ślepo starych współrzędnych. Prawdziwa późniejsza korekta fizyczna wycofuje sprzeczne dopasowane etykiety.
6. **Opóźnienie Current było też w UI.** Zwykłe odświeżanie co 4 s najpierw czekało na status i diagnostykę. Dodano niezależny odczyt około 0,5 s, lekki bootstrap kart, brak nakładania żądań i krótszą sekcję blokady przy rekomendacjach. Test sprawdza odczyt Current podczas niezatwierdzonej transakcji treningu w SQLite/WAL.

## Co jest RL, a co nim nie jest

Bazowy model jest kontekstowym bandytą z głowami LinUCB i aktualizacjami nagród; nie jest pełnym wielokrokowym modelem planowania środowiska. Ręcznie podane Desired jest etykietą preferencji. Nowa warstwa to odwracalna pamięć kontekstowych demonstracji użytkownika, a nie udawany pomiar nagrody ze środowiska. Opcjonalne eksperymenty nadal mają osobną pętlę obserwacji wyników; są pomijane, gdy decyzję wyznacza pasująca ręczna etykieta.

Nagroda za brak ręcznej korekty po akcji jest przybliżeniem akceptacji, nie dowodem zadowolenia. Wysoki benchmark odtwarzania historii również nie dowodzi komfortu ani oszczędności. Nie zwiększano sztucznie tych metryk po dodaniu etykiet.

## Ograniczenia świadomie zachowane

- Maksymalnie 256 aktywnych etykiet na agenta. Dopasowanie dotyczy aktualnie wybranych cech; ręczna etykieta nie dowodzi, że wybrano najlepsze możliwe sensory.
- Kontekst musi być dostępny. Inny schemat, brak sensora, inny stan ruchu albo zmiana znaczenia opcji powoduje brak dopasowania. Etykiety nie są globalnym wymuszeniem stanu urządzenia.
- Undo nie cofa fizycznej historii ani pośrednich skutków dawnych akcji; nie usuwa niezależnego treningu na rzeczywiście zarejestrowanych zdarzeniach. Nie można odtworzyć selektywnego undo starych korekt, których poprzednia wersja nie przechowywała osobno.
- Nie przeprowadzono eksperymentu w domu użytkownika ani pomiaru Raspberry Pi. Należy oceniać poprawność etykiet na rozłącznych fragmentach historii i sprawdzać liczbę późniejszych ręcznych korekt, zamiast interpretować samą pewność jako jakość.

Dowody wykonania: tests/test_history_teaching.py, tests/test_live_frontend.py oraz TEST_RESULTS_0_11_0.txt. Bez wywołań prawdziwych urządzeń podczas testów.
