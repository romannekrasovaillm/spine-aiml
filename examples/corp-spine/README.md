# Образец корп-спайна (MVP наследования CONSTRAINTS)

Демо механики `docs/corp-spine.md`: корпоративный уровень
(`CONSTRAINTS.corp.yaml`, владелец ДКА, `version: "2026.3"`) и продуктовый
репозиторий-фикстура (`product/`) с `extends:` и override через ADR.

Прогон из корня этого репозитория:

```bash
arch-be control check examples/corp-spine/product \
  --constraints examples/corp-spine/product/CONSTRAINTS.yaml
arch-be control report examples/corp-spine/product \
  --constraints examples/corp-spine/product/CONSTRAINTS.yaml --level corp --json
```

Базовый сценарий — PASS: deny-hit по `left-pad` в `product/Cargo.toml`
покрыт активным override (ADR-041, until 2027-01); источники правил и
статусы overrides видны в выводе. Сценарии «родитель обновился» и
«override истёк» — в `docs/corp-spine.md` (§ «Сценарии для демо»).
