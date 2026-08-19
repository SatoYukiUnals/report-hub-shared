// レポート内の確認事項を、ページ末尾の「回答する」1 つでまとめて保存する。
//
// 使い方（レポート HTML 側）：設問の箱を並べるだけでよい。ボタンは書かない。
//
//   <div class="qa" data-qa-id="wbs-2.4" data-question="WBS 2.4 の扱い">
//     <h3>WBS 2.4 をどうするか</h3>
//     <p class="why">対象画面が消えたため、そのままでは実施できない。</p>
//     <label><input type="radio" name="choice" value="取消">取消にする</label>
//     <label><input type="radio" name="choice" value="書き換え">対象を書き換える</label>
//     <textarea name="note" placeholder="補足（任意）"></textarea>
//   </div>
//   <script src="/assets/answers.js"></script>
//
// 送信先は URL から決める（/r/<プロジェクト>/<名前>.html → /api/answers/<プロジェクト>/<名前>）。
// 同じ設問には何度でも答えられ、最後の回答だけが残る。
//
// 古いレポートが設問ごとに <form class="qa"> と送信ボタンを持っていても動く。
// その場合ボタンは隠し、末尾の 1 つへ集約する。
(() => {
  const match = location.pathname.match(/^\/r\/([^/]+)\/(.+)\.html$/)
  if (!match) return
  const endpoint = `/api/answers/${match[1]}/${match[2]}`

  const boxes = () => Array.from(document.querySelectorAll('.qa[data-qa-id]'))

  /** 選択肢を設問ごとに独立させる。
   *
   * form が無いので name="choice" のままだと、ページ内の全設問が 1 つの
   * ラジオグループになり、どれか 1 問しか選べない。設問 ID を混ぜて分ける。
   */
  const isolate = (box) => {
    for (const input of box.querySelectorAll('input[type="radio"]')) {
      input.name = `choice:${box.dataset.qaId}`
    }
  }

  /** 設問の中の値を読む。未選択・空欄なら null（＝まだ答えていない） */
  const readBox = (box) => {
    const checked = box.querySelector('input[type="radio"]:checked')
    const note = box.querySelector('[name="note"]')
    const choice = checked ? checked.value : ''
    const text = note ? note.value.trim() : ''
    if (!choice && !text) return null
    return {
      qa_id: box.dataset.qaId,
      question: box.dataset.question || '',
      choice,
      note: text,
    }
  }

  /** 設問ごとの状態表示（保存済みの内容をその場に出す） */
  const statusLine = (box) => {
    let status = box.querySelector('[data-qa-status]')
    if (!status) {
      status = document.createElement('p')
      status.setAttribute('data-qa-status', '')
      status.className = 'qa-status'
      box.appendChild(status)
    }
    return status
  }

  const showSaved = (box, entry) => {
    const note = entry.note ? `／${entry.note}` : ''
    statusLine(box).textContent =
      `回答済み：${entry.choice || '(選択なし)'}${note}　${entry.answered_at}`
    box.dataset.qaSaved = '1'
  }

  // ---------------------------------------------------------------- 回答バー
  const bar = document.createElement('div')
  bar.className = 'qa-bar'
  const count = document.createElement('span')
  count.className = 'qa-bar-count'
  const message = document.createElement('span')
  message.className = 'qa-bar-message'
  const button = document.createElement('button')
  button.type = 'button'
  button.textContent = '回答する'
  bar.append(count, message, button)

  const refreshCount = () => {
    const all = boxes()
    const answered = all.filter((box) => readBox(box) || box.dataset.qaSaved).length
    count.textContent = `${all.length} 件中 ${answered} 件`
    const left = all.length - answered
    count.className = left ? 'qa-bar-count is-open' : 'qa-bar-count'
  }

  // ---------------------------------------------------------------- 保存
  const save = async () => {
    const entries = boxes().map(readBox).filter(Boolean)
    if (!entries.length) {
      message.textContent = '選んでから回答する。'
      return
    }
    button.disabled = true
    message.textContent = ''
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers: entries }),
      })
      if (!res.ok) throw new Error(String(res.status))
      const data = await res.json()
      for (const entry of data.saved || []) {
        const box = document.querySelector(`.qa[data-qa-id="${entry.qa_id}"]`)
        if (box) showSaved(box, entry)
      }
      message.textContent = `${(data.saved || []).length} 件を保存した。`
      refreshCount()
    } catch {
      message.textContent = '保存できなかった。サーバーが動いているか確認して、もう一度送る。'
    } finally {
      button.disabled = false
    }
  }

  /** 保存済みの回答を画面へ戻す（再読み込みしても消えないように） */
  const restore = async () => {
    let saved = []
    try {
      const res = await fetch(endpoint)
      if (res.ok) saved = await res.json()
    } catch {
      // サーバーが止まっていても本文は読めるようにする
    }
    for (const entry of saved) {
      const box = document.querySelector(`.qa[data-qa-id="${entry.qa_id}"]`)
      if (!box) continue
      const radio = Array.from(box.querySelectorAll('input[type="radio"]'))
        .find((input) => input.value === entry.choice)
      if (radio) radio.checked = true
      const note = box.querySelector('[name="note"]')
      if (note && entry.note) note.value = entry.note
      showSaved(box, entry)
    }
    refreshCount()
  }

  const start = () => {
    const all = boxes()
    if (!all.length) return
    for (const box of all) isolate(box)
    // 設問ごとの送信ボタンは末尾の 1 つへ集約する（古いレポート向け）
    for (const old of document.querySelectorAll('.qa button')) old.hidden = true
    document.body.appendChild(bar)
    button.addEventListener('click', () => void save())
    document.addEventListener('change', (event) => {
      if (event.target instanceof Element && event.target.closest('.qa')) refreshCount()
    })
    // 設問ごとの form が残っていても、送信でページが遷移しないようにする
    document.addEventListener('submit', (event) => {
      if (event.target instanceof Element && event.target.classList.contains('qa')) {
        event.preventDefault()
        void save()
      }
    })
    void restore()
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start)
  } else {
    start()
  }
})()
