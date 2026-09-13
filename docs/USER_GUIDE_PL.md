> Dokument archiwalny sprzed 0.9. Aktualna wersja: [obsługa](QUICK_START_PL.md), [architektura](ARCHITECTURE_0_9.md).

# Obsługa Adaptive AI

## Główny ekran

### Status połączenia

Na górze aplikacji widać połączenie z Home Assistant. Normalnie powinien być aktywny strumień realtime oparty o WebSocket. Gdy realtime nie działa, aplikacja może korzystać z odczytu REST jako fallbacku.

### Panel historii

Panel pokazuje m.in.:

- aktualną fazę,
- procent postępu,
- ETA,
- liczbę zmian w lokalnym archiwum,
- liczbę encji,
- zakres historii,
- liczbę wykrytych celów.

### Rescan devices

`Rescan devices` ponownie sprawdza aktywnie używane, obsługiwane cele sterowania i może utworzyć nowych agentów.

Użyj po dodaniu nowego urządzenia albo gdy istniejący cel nie został wykryty.

## Karta agenta

### Current

Aktualna wartość odczytana z Home Assistant.

### Desired

Wartość, którą polityka aktualnie uważa za właściwą.

W Shadow Desired jest tylko prognozą. W Control może stać się komendą.

### Candidate confidence

Pokazywany przede wszystkim podczas/po historycznym benchmarku. To nie jest chwilowa pewność jednej decyzji, lecz wynik historycznej zdolności agenta do odtwarzania zachowania celu.

### Live confidence

Po kwalifikacji i uruchomieniu inference aplikacja pokazuje bieżącą, kalibrowaną pewność decyzji.

### Behaviour benchmark

Najważniejszy wynik kwalifikacji. Domyślny próg to **>78%**.

Dla binarnych celów ON/OFF liczone są oba kierunki tak, aby dominujący stan OFF nie zawyżał wyniku.

### Support

Informuje, jak dużo historycznego wsparcia ma podobny kontekst. Niski support oznacza, że bieżąca sytuacja była rzadko obserwowana.

### Novelty

Miara nietypowości aktualnego kontekstu. Wysoka novelty oznacza, że model działa poza dobrze poznanym zakresem.

## Tryby agenta

### Shadow

- inference jest aktywne,
- Desired jest aktualizowane,
- brak komend sterujących,
- najlepszy tryb do walidacji.

### Control

- agent może bezpośrednio wywoływać usługi HA,
- dostępny tylko po kwalifikacji historycznej,
- dodatkowo stosowane są bramki confidence/support/novelty, timingi i zabezpieczenia konfliktów.

### Paused

- brak normalnego realtime inference,
- brak normalnego bieżącego treningu,
- minimalne użycie CPU przez danego agenta.

Agent może być PAUSED automatycznie po nieudanym benchmarku lub można ręcznie ustawić tryb `paused`.

## Resume

`Resume` jest przeznaczony dla agenta w stanie PAUSED.

Resume:

- nie usuwa modelu,
- nie usuwa benchmarku,
- nie usuwa doświadczeń,
- nie zeruje kursora,
- kontynuuje indeksację od ostatniego zapisanego punktu, z niewielkim overlapem.

Używaj, gdy od poprzedniej kwalifikacji pojawiły się nowe dane i chcesz sprawdzić, czy model zyskał wystarczającą jakość.

## Rebuild

`Rebuild` to pełna przebudowa pojedynczego agenta.

Rebuild:

- czyści model,
- czyści wynik benchmarku,
- zeruje kursor treningu,
- przebudowuje doświadczenia agenta,
- zachowuje surowe lokalne archiwum,
- odświeża szerszy zestaw kandydatów z Recorder.

Używaj szczególnie gdy:

- dodano nowy sensor,
- zmieniono integrację sensora,
- zmieniono ręcznie listę context entities,
- zmieniono zakres sterowania,
- podejrzewasz, że model wybrał zły kontekst.

Nie używaj Rebuild jako zwykłego sposobu wznowienia nauki – od tego jest Resume.

## Ustawienia agenta

Przycisk `Ustawienia` pozwala zmieniać m.in.:

- minimum i maksimum nastawy,
- deadband,
- wymagane confidence,
- minimalny odstęp między komendami,
- timeout potwierdzenia,
- czas stabilizacji,
- czas priorytetu ręcznej zmiany,
- listę encji kontekstu.

`*` w polu kontekstu oznacza automatyczny dobór ze wszystkich dopuszczonych kandydatów.

Zmiana zakresu lub kontekstu resetuje model danego agenta i wymaga ponownej nauki.

## Explore

`Explore` włącza ograniczoną mikroeksplorację. Funkcja jest dostępna tylko dla zakwalifikowanych agentów i może celowo testować pobliskie akcje.

Dla pierwszych wdrożeń pozostaw ją wyłączoną.

## Delete

Usuwa agenta i jego model. Auto-discovery może go później utworzyć ponownie, jeżeli urządzenie nadal spełnia kryteria.
