# Lifecycle agentów

## Stany treningowe

### TRAINING

Agent indeksuje historię od początku dostępnego zakresu lub od zapisanego kursora.

W tym stanie:

- polityka może być przebudowywana,
- benchmark jest aktualizowany,
- normalne realtime inference jest wstrzymane,
- Control jest niedostępny.

Domyślnie historia jest przetwarzana porcjami około **48 godzin**, z około **12 godzinami overlapu** między porcjami. Po każdej porcji zapisywany jest kursor.

### QUALIFIED

Agent przeszedł pełny historyczny benchmark z wynikiem **>78%** i odpowiednią liczbą próbek.

Po kwalifikacji automatycznie przechodzi do **Shadow**.

QUALIFIED oznacza, że agent może być kandydatem do Control. Nie oznacza, że Control musi być od razu włączony.

### PAUSED

Agent zakończył pełny pass, ale nie spełnił kryteriów, albo został ręcznie zatrzymany.

W tym stanie ograniczamy zużycie CPU:

- brak normalnego realtime inference,
- brak regularnego dotrenowywania,
- brak Control.

## Resume

Resume kontynuuje od zapisanej pozycji.

Przykład:

```text
1 Sep ---------------- 10 Sep -------- 12 Sep
                       ↑
                 zapisany cursor
```

Po Resume aplikacja nie zaczyna od 1 Sep. Pobiera/przetwarza dane w pobliżu kursora i idzie do bieżącego czasu.

Zachowane są:

- model,
- benchmark,
- historyczne doświadczenia,
- Selected context,
- kursor.

Po dojściu do teraz agent ponownie przechodzi kwalifikację.

## Rebuild

Rebuild oznacza pełną przebudowę agenta.

Resetuje:

- politykę,
- benchmark,
- training cursor.

Nie usuwa surowego lokalnego archiwum całej aplikacji.

Podczas Rebuild wykonywane jest szersze sprawdzenie kandydatów z Recorder, aby nowo dodane sensory mogły zostać zauważone.

### Kiedy używać Rebuild

- nowy sensor w domu,
- zmiana sposobu działania sensora,
- migracja ESPHome,
- poprawiona encja presence,
- ręczna zmiana context entities,
- stary Selected context jest ewidentnie błędny,
- duża zmiana automatyki domu.

### Kiedy nie używać Rebuild

Nie używaj tylko po to, aby dać słabemu agentowi kolejną szansę na nowych danych. W takim przypadku użyj **Resume**.

## Po restarcie aplikacji

Kursor treningu jest zapisywany w bazie. Restart podczas długiej indeksacji nie powinien wymuszać pełnego liczenia od początku; zadanie może być kontynuowane od checkpointu.
