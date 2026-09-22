## Что и зачем

<!-- Ссылка на issue/ADR. Одно-два предложения. -->

## Проверки (обязательны)

- [ ] `cargo fmt --all` — чисто
- [ ] `cargo clippy --all-targets -- -D warnings` — чисто
- [ ] `cargo test` — зелёный (новое поведение покрыто тестами)
- [ ] dogfood: `arch-ml control check . --constraints CONSTRAINTS.yaml` — PASS
- [ ] Документация обновлена (`docs/`, README при изменении UX)
- [ ] Без секретов и персональных путей (CI-сканы)
- [ ] Значимость Standard/Critical — есть ADR в `docs/adr/`
