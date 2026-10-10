# HomeMind Adaptive AI 0.14.168

Można wskazać dodatkowy czujnik jasności dla agenta światła, także z innego obszaru HA. Sygnał pozostaje w konfiguracji, historii treningu i modelu Candidate; nie zastępuje dotychczasowego sensora obecności. Sensor Tournament nie może samoczynnie go usunąć.

## Konfiguracja

1. Zainstaluj aktualizację i uruchom dodatek ponownie.
2. Na wybranej generacji agenta otwórz **Explore → Dodatkowa jasność**.
3. Wybierz encję czujnika oraz cel:
   - **Dodatkowy kontekst treningu**: model dostaje nową informację, a historia nadal opisuje dotychczasowe zachowanie.
   - **Pomijaj włączenie, gdy jest wystarczająco jasno**: dodatkowo deklarujesz zmianę pożądanego zachowania.
4. Przy drugim celu ustaw próg, margines histerezy i maksymalny wiek odczytu. Próg jest w jednostkach wybranego czujnika: lx albo surowej skali LD2410. Dla LD2410 odczyt jasności wymaga odpowiedniej konfiguracji engineering.
5. Powstanie nowy Candidate i pełny Rebuild. Bieżąca generacja Live zachowuje swój model i ustawienia. Candidate porównuje decyzje w pasywnym Shadow. **Promote** przenosi model i dodatkowy sygnał razem, dopiero po spełnieniu warunków promocji.

Sam wybór czujnika nie określa, od jakiej jasności schody mają pozostać ciemne. W tym wydaniu próg określa użytkownik; automatyczne uczenie optymalnego progu nie jest zaimplementowane. Historia starej automatyzacji opartej wyłącznie na ruchu nie zawiera takich etykiet.

## Decyzje i ocena

Preferencja blokuje tylko nowe włączenie przy potwierdzonej jasności. Nie wymusza wcześniejszego OFF podczas pobytu. Ręczne polecenia mają pierwszeństwo. Odczyt niedostępny, za stary, z przyszłości lub ze zmienioną jednostką pozostawia decyzję zwykłemu modelowi. Histereza utrzymuje poprzedni wynik w paśmie progu, dopóki odczyty są poprawne; utrata poprawnego pomiaru kasuje tę pamięć.

Porównanie zachowuje rzeczywistą historię działań. Ocenę zadeklarowanej preferencji zapisuje osobno, bez wytwarzania fikcyjnych demonstracji OFF i bez wykorzystywania własnych poleceń jako potwierdzenia. Promocja preferencji wymaga co najmniej czterech przyszłych okazji dziennych i czterech wejść w ciemności. Skuteczność w ciemności nie może spaść o więcej niż trzy punkty procentowe względem rodzica. Pozostałe warunki promocji oraz uprawnienia Control nadal obowiązują.

Shadow pamięta hipotetyczny stan Candidate. Dzięki temu włączenie przez starą automatyzację nie zamienia pominiętego przez Candidate włączenia w jego własne ON. Cel kontekstu bez preferencji nadal korzysta ze zwykłego porównania zachowania.

Czujnik z kuchni musi reprezentować jasność na schodach. Jeżeli jego wynik mocno zmienia światło kuchenne, wybierz lepszy sygnał lub dostosuj konfigurację pomiaru. Kod nie przenosi progu kuchennej automatyzacji na schody i nie wylicza automatycznie korekty między pomieszczeniami.

## Poprawka treningu i walidacja

Poprawiono wybór historycznego punktu decyzji: wcześniejszy czas zdarzenia sensora nie może używać stanu, który dotarł dopiero później. Gdy wskazówka nie była jeszcze dostępna, trening korzysta z czasu rzeczywistej zmiany urządzenia. Chroni to prawidłowe OFF przed oceną w kontekście poprzedniego pobytu.

Dodano 19 testów: semantyka sygnału, świeżość, jednostki, histereza, ochrona sensora, tworzenie i restart Candidate, zmiany konfiguracji, pasywny Shadow, osobne oceny, wymagania ciemności oraz atomowa promocja i rollback. Test rzeczywistego izolowanego treningu na syntetycznej historii wyłącznie ruchowej sprawdza dzienne pominięcie ON, wejście w ciemności, nieruchomą obecność oraz pozostawienie już zapalonego światła. Wynik filtra preferencji jest oddzielony od surowej predykcji wytrenowanego modelu.

Pełny zestaw regresji obejmuje 2009 testów. Walidacja nie jest pomiarem skuteczności w Twojej instalacji HA; potrzebne są przyszłe obserwacje Shadow dla wybranego czujnika i progu.
