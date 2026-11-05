# fog-feeds

北海道の釣果・釣行情報を複数のサイトから集め、RSS にまとめる。釣りアプリ Fog の「釣果情報」に登録して使う。

- RSS（北海道全域）: `https://raw.githubusercontent.com/knowlsaws/fog-feeds/main/docs/hokkaido.xml`
- RSS（函館周辺だけ）: `https://raw.githubusercontent.com/knowlsaws/fog-feeds/main/docs/hakodate.xml`
- 一覧ページ: `docs/index.html`（GitHub Pages を有効にすると `https://knowlsaws.github.io/fog-feeds/`）
- JSON: `docs/hokkaido.json` / `docs/hakodate.json`

## 仕組み

- `collector/sources.json` の取得元（RSS、RSS の無いサイトのリンク一覧、自動判定）を 3 時間ごとに読む（`.github/workflows/collect.yml`）
- 北海道・地域名・道内の港名や地名（`collector/places.json`、約 480 件）を含む記事だけ残す（`needRegion` の取得元）。ニュース検索は釣りの語も必要。函館から 120 km 以内の地名を含む記事は `hakodate.xml` にも入る
- 新しい記事はページを 1 回だけ読んで、説明（meta description か本文の冒頭）と日付を補う
- 場所と魚種（`collector/species.json`）を文中から拾い、説明の末尾に「場所: … / 魚種: …」と書く（Fog の地図ピンが使う）
- 状態は `data/items.json` に 90 日・最大 1,500 件。出力は `docs/`

## 取得元を足す

`collector/sources.json` に 1 行足す。

- `type`: `rss`（RSS/Atom）/ `html`（ページのリンク一覧）/ `auto`（RSS を試し、ページが指す RSS を探し、無ければリンク一覧）
- `filter`: リンク一覧のとき、URL にこの文字を含むリンクだけ（例 `/report/`）
- `needRegion`: 北海道の語を含む記事だけにする（全国サイト向け）
- `noEnrich`: 記事ページを読まない（説明がサイト共通の文になるサイト向け）
- `needFishing`: 釣りの語を含む記事だけにする（ニュース検索向け）
- `fallback`: 最初の URL が読めないときに試す URL

## 取得元サイトへの配慮

各サイトへのアクセスは 3 時間に 1 回、記事ページは新着だけ最大 25 件、間隔 0.5 秒。User-Agent に `Fog-feeds` とこのリポジトリの URL を明記する。本文は保存せず、説明は 400 文字まで。
