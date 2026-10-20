# 0.14.221: Resume i Rebuild w osadzonym panelu

Wersja rozwija 0.14.220. Końcowa walidacja: **2749 testów OK / 374.252 s lokalnie**, entrypoint, oba UI, compileall i 21 plików JS. Dokładny kod wykonawczy `898bffa2` przeszedł CI na Pythonie 3.11/3.13, dodatkowe kontrole workflow i obraz Docker.

Eksport 0.14.220 z 20 października pokazuje pustą kolejkę (rewizja 0, brak przejść i aktywnych zadań), działający wątek kolejki i brak blokad zapisu. Agent ma stan paused, zapisany częściowy model i zgodną sumę kontrolną modelu. Użytkownik potwierdził brak jakiejkolwiek reakcji obu przycisków. Pliki wejściowe są traktowane wyłącznie jako dane; ich hashe i kompaktowe fakty są w `benchmarks/TRAINING_BUTTONS_0_14_221_INPUTS.json`.

Oba przyciski korzystały z natywnego `confirm()`. Jeżeli przeglądarka tłumi to okno w osadzonym panelu, kod kończy się przed wysłaniem HTTP. Brak komunikatu i brak wpisu w kolejce pasują do tej ścieżki. Odtworzenie na kodzie 0.14.220 z confirm zwracającym false daje zero żądań po obu akcjach. Nie mamy bezpośredniego zrzutu konsoli przeglądarki użytkownika; nie przedstawiamy tłumienia modali jako niezależnie zaobserwowanego faktu na jego urządzeniu.

Blokowanie natywnych alert/confirm przez sandbox ramki opisuje [HTML Standard](https://html.spec.whatwg.org/multipage/browsers.html). Nie wymaga to uszkodzenia modelu ani kolejki.

## Poprawka

- Resume i Rebuild używają dialogu HTML w aplikacji, bez zależności od natywnego confirm lub alert. Dialog pozostaje poza odświeżanymi kafelkami.
- Rebuild nadal wymaga jawnego potwierdzenia wyczyszczenia modelu, benchmarku i kursora. Anulowanie nie wysyła żądania. Resume zachowuje istniejące dane nauki.
- Wysyłanie, przyjęcie do kolejki i błędy serwera są widoczne w dialogu. Powtórne kliknięcia podczas wysyłania lub po akceptacji nie wysyłają drugiego żądania. Wyjątek odświeżania kafelków nie zmienia udanego przyjęcia w błąd treningu.
- Test świeżo otwartej bazy z przerwanym stanem training sprawdza zmianę na paused bez utraty modelu, benchmarku i kursora. Resume i jawny Rebuild przechodzą przez rzeczywisty rejestr tras i TrainingQueue; start workera jest stubem. To test wznowienia procesu/admission, nie odcięcia zasilania sprzętu ani całego replay.

Backend treningu, limity dysku/RAM, bramki Control oraz sposób zapisywania checkpointów pozostają bez zmian. Restart nadal nie uruchamia automatycznie ciężkiego treningu.

Po aktualizacji i przebudowaniu dodatku odśwież panel. Aby kontynuować przerwany trening, wybierz **Resume training**, a następnie potwierdź w widocznym oknie aplikacji. Rebuild wybieraj wtedy, gdy zamierzasz rozpocząć uczenie tego modelu od początku. Przy błędzie serwera dialog pokaże jego treść. Full Rebuild nie jest wymagany do samej instalacji poprawki.

Końcowy manifest 633 plików kodu, testów i narzędzi jest zgodny z zamrożonymi źródłami lokalnej walidacji i opublikowanym kodem. Ostatni commit zmienia wyłącznie raporty oraz metadane. PR: https://github.com/mpuchara/HomeMind-HA/pull/337. CI: https://github.com/mpuchara/HomeMind-HA/actions/runs/39445817804.
