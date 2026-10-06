# Контент-завод AUQNI: карта среды

Этот файл фиксирует подтверждённые сведения, но не заменяет живую проверку Git
и production-состояния перед работой или деплоем.

| Поле | Значение |
| --- | --- |
| Production environment | HSHP |
| Production workspace | `/home/anatoly/projects/auqni/ПРОЕКТЫ/Контент-завод_AUQNI` |
| Локальная Git-копия | `C:\Users\muzen\auqni\ПРОЕКТЫ\Контент-завод_AUQNI_GITCHECK` |
| Remote | `https://github.com/anatolymuzenik-ops/auqni-content-factory.git` |
| Ветка | `main` |
| Подтверждённый production/deploy commit | `a0b5f31` |
| Статус синхронизации | На 06.10.2026 ноутбук, GitHub и HSHP подтверждённо совпадают по commit `a0b5f31` |
| Skill на HSHP | Рабочий `auqni-content-orchestrator` подтверждённо совпадает с канонической Git-версией по SHA-256 |
| Дата проверки | 06.10.2026 |
| Дата последнего деплоя | Не установлена; дата проверки не означает дату деплоя |

Runtime-данные и секреты хранятся отдельно от Git. Skills и agents в репозитории
считаются каноническими; рабочие копии на уровне workspace нужно сверять с ними
при подготовке развёртывания.
