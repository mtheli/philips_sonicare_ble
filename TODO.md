# Philips Sonicare BLE — TODO

## Sektor-Berechnung

- [ ] Mode-abhängige Sektor-Sequenz für Tuscany-Premium statt gleichmäßiger Aufteilung
      (siehe `~/.claude/projects/-home-theli-reverse-engineering/memory/sonicare_sector_sequences.md`).
      White+ = 8 Schritte (1,2,3,4,5,6,2,5), Gum Health = 10 (1,2,3,4,5,6,1,3,4,6),
      Deep Clean+ = 6×30s, Rest = 6×20s, Tongue Care ohne Sektoren.
      Default-Startquadrant = 1 (App-Preference nicht über BLE exponiert → ggf. Drift).
      Fallback auf gleichmäßige Aufteilung für Kids/Condor/unbekannte Modes.

## Success-Banner nach Session-Ende

- [ ] Sektor-Sensor soll nach Abschluss einer Session persistent `"success"` melden
      (analog Oral-B), damit die Card das "Brushing complete!"-Banner zeigt.
      Problem: Sonicare resettet `brushing_time` nach Session-Ende auf 0 und trennt
      die BLE-Verbindung, `brushing_state` wird `None`. Rein wertebasierte Logik
      reicht nicht.
      Optionen: (1) stateful Flag `_session_complete` im Sensor mit Reset bei neuer
      Session, (2) zusätzlich `RestoreEntity` für HA-Neustart, (3) optional Timeout
      (z.B. 5 min) damit Banner nicht tagelang hängt.
      Nachteile je Option diskutiert — Entscheidung offen.
