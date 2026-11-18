# Резервные копии удалённых приложений

Перед каждым удалением APK снимается через ADB и складывается в `apk-backup/`
с именем `<package>.apk`. Откат — `pm install` этого файла (см. `docs/УСТРОЙСТВО-smart-project.md`).

**В репозитории самих APK нет.** Это чужие сборки общим объёмом ~436 МБ, и GitHub
не принимает файлы больше 100 МБ (Chrome — 102 МБ). Здесь только список: по нему видно,
что было удалено и что можно вернуть со своей копии.

| Пакет | Размер | Восстановление |
|---|---|---|
| `cm.aptoidetv.pt` | 11.5 МБ | `pm install cm.aptoidetv.pt.apk` |
| `com.android.chrome` | 102.3 МБ | `pm install com.android.chrome.apk` |
| `com.droidlogic.mediacenter` | 3.9 МБ | `pm install com.droidlogic.mediacenter.apk` |
| `com.droidlogic.musicplayer` | 4.7 МБ | `pm install com.droidlogic.musicplayer.apk` |
| `com.droidlogic.videoplayer` | 10.3 МБ | `pm install com.droidlogic.videoplayer.apk` |
| `com.google.android.leanbacklauncher` | 603.0 КБ | `pm install com.google.android.leanbacklauncher.apk` |
| `com.master.tv.market` | 7.5 МБ | `pm install com.master.tv.market.apk` |
| `com.smart.tv.launcher` | 19.1 МБ | `pm install com.smart.tv.launcher.apk` |
| `com.softonic.hub` | 64.6 МБ | `pm install com.softonic.hub.apk` |
| `com.varmatch.sports` | 80.7 МБ | `pm install com.varmatch.sports.apk` |
| `ru.more.play` | 43.3 МБ | `pm install ru.more.play.apk` |
| `ru.vk.store.tv` | 37.8 МБ | `pm install ru.vk.store.tv.apk` |
| `ru.yandex.music` | 49.6 МБ | `pm install ru.yandex.music.apk` |

## Как снять бэкап заново

```bash
TV_HOST=192.168.1.50 python3 tools/backup.py
```

Скрипт складывает APK и снимок состояния (`packages_all.txt`, `packages_disabled.txt`,
`props.txt`) в `~/.projector/backups/<дата>/`.
