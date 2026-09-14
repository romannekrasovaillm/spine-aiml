---
id: AD-6
type: ad
title: "Судья не участвует в награде"
status: "PROPOSED"
affects: [C-013]
verified_by: [C-013]
source: "docs/adr/ADR-001-predmet-revizii-laguny-os-uspeha-i-obyom-delty.md"
---

- **Binds**: eval-контур (GLM-5.2) ↔ контур награды RL ↔ отчётность
- **Prevents**: reward hacking — оптимизацию судьи вместо задачи, когда метрика растёт, а качество падает (у Лагуны судья уже вынесен в eval, но это нигде не защищено правилом)
- **Rule**: награда RL — детерминированная функция исполнения (parse / min_steps / timeout / relevance / tool_error / verifier); LLM-судья вызывается только на eval-стадии и никогда внутри шага обучения; расхождение «reward растёт при падении `judge_mean`» трактуется как сигнал хак-а и блокирует заявление об успехе. Страж: C-013.
- **Статус**: [PROPOSED]
