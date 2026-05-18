# New Shoot Workflow

新しい撮影データを Resolve 用に整理するときの短い運用メモです。
細かい仕様は忘れていてよく、まずはこの順番で進めます。

## 1. 撮影後に用意するもの

外付け SSD などに、撮影ごとに1つフォルダを作ります。

```text
<撮影名>/
└── incoming/
    ├── camera files...
    ├── camera files...
    └── master audio files...
```

ルール:

- 動画と master audio は、まず全部 `incoming/` に入れる。
- ファイル名はランダムでもよい。
- take ごとの仕分けや angle 名付けは Codex に任せる。
- 元素材を他にも保存している場合でも、この作業フォルダ内に必要な動画・音声が残る状態にする。

## 2. Codex への頼み方

まず repo を開いて、こう依頼します。

```text
このフォルダを davinci-session-bootstrap の skill で処理してください。
session root は <撮影フォルダのパス> です。
incoming/ から group-session, prepare-resolve-session, inspect-resolve-session,
operator-handoff まで実行してください。
labeling も E2E acceptance として確認してください。
結果は Markdown report を中心に要約してください。
```

例:

```text
session root は /Volumes/PortableSSD/2026xxxx_撮影名 です。
skill 指定で最後までお願いします。
```

## 3. Codex が実行する標準ステージ

Codex は基本的に以下を実行します。

```bash
./scripts/pg group-session "<session-root>" --json
./scripts/pg prepare-resolve-session "<session-root>" --json
./scripts/pg inspect-resolve-session "<session-root>" --json
./scripts/pg operator-handoff "<session-root>" --json
```

`prepare-resolve-session` は Resolve project を作成し、`00_color_prep_all_takes`
を作ります。さらに保存・閉じる・再読込後の検証も行います。

## 4. 最初に読む Markdown

JSON ではなく、まず Markdown を見ます。

```text
reports/auto-group-plan.md
reports/take-order.md
reports/prepare-resolve-session.md
reports/inspect-resolve-session.md
reports/operator-handoff.md
```

見るポイント:

- `auto-group-plan.md`: take 数、angle label、lane ID、source filename。
- `take-order.md`: 撮影順の推定。同じ angle 内のファイル時刻が主な根拠。フォルダ名は勝手に変更しない。
- `prepare-resolve-session.md`: post-reload verification、sync confidence。
- `inspect-resolve-session.md`: color prep timeline が存在し、6 take など期待数があるか。
- `operator-handoff.md`: Resolve で次に何をするか。

`WARN` は作業続行可能なことが多いですが、内容は必ず確認します。
`FAIL` は止めて Codex に原因調査を依頼します。

## 5. Resolve での作業

Resolve では `00_color_prep_all_takes` を開きます。

最初に Project Settings を確認します。

- Timeline frame rate: `29.97`
- Playback frame rate: `29.97`
- Input color space: `Rec.2100 HLG`
- Timeline / Output color space: `Rec.709 Gamma 2.4`

特に Playback frame rate は Resolve scripting API で直せないことがあるので、
`24` などになっていたら手動で `29.97` に直します。

基本:

- `compact-v1`, `compact-v2`, ... は詰め込み行。
- 実際の画角名は clip item の `angle-a`, `angle-b`, ... を見る。
- A1 は master audio のみ。
- camera scratch audio は timeline に載せない。
- Color Page では Local Grade を使う。
- 同じ angle の別 take へは Gallery Still / Apply Grade を使い、最後は take ごとに微調整する。

## 6. よくある WARN

### `timelinePlaybackFrameRate`

Resolve scripting API では直せないことがあります。
Resolve の Project Settings で手動で `29.97` など撮影設定に合わせます。
今回の標準運用では Timeline frame rate / Playback frame rate の両方を
`29.97` にします。

### `sync_confidence` が低い

自動配置はされていますが、音声同期の確信度が低めです。
該当 take / angle を Resolve 上で目視・聴感確認します。

## 7. Color Prep 後の Multicam 化

色調整が終わったら、元 timeline を直接壊さずに進めます。

1. `00_color_prep_all_takes` を保存用として残す。
2. 色調整済み timeline を複製する。
   - 例: `color-prep-v1`
3. さらに take ごとに複製して、対象 take 以外を削除する。
   - 例: `take-01_mc_source`
4. Media Pool の `Timelines` で、その take 用 timeline を右クリックする。
5. 以下を選ぶ。

```text
Convert Timeline to Multicam Clip
→ Use Reference Audio/Angle 1
```

6. できた multicam clip を新しい edit timeline に置く。
   - 例: `take-01_edit`
7. Multicam Viewer を表示して angle switching する。

確認ポイント:

- color grade が残っているか。
- A1 が `audio-master` になっているか。
- camera scratch audio が混ざっていないか。
- sync がズレていないか。
- angle switching できるか。

まず take-01 だけで試し、問題なければ他 take に展開します。

## 8. 作業後の整理

一段落したら、残すフォルダを1つに決めます。

残すべきもの:

- `takes/` の動画・音声
- `reports/` の Markdown
- `resolve/` の `.drp`
- 必要なら `Resolve Project Library`

不要な E2E test folder や cache はゴミ箱へ移動します。
削除はすぐにせず、数日後に問題がないことを確認してから判断します。

## 9. 困ったときの頼み方

```text
reports/prepare-resolve-session.md と inspect-resolve-session.md を見て、
WARN / FAIL の意味と次にやることを説明してください。
```

```text
00_color_prep_all_takes が期待通りか、Resolve API で直接確認してください。
take 数、marker 数、track 数、media offline の有無を見てください。
```

```text
この撮影フォルダを整理してください。
素材が残ることを確認し、不要な開発・test folder はゴミ箱へ移動してください。
```
