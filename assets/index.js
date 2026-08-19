// レポート一覧（受信箱型）の絞り込みと更新。
//
//   ・上のタブ（未回答 / 新着 / すべて）と左のプロジェクト、検索欄で行を絞る
//   ・J / K で行を移動、Enter で開く、/ で検索へ、Esc で検索を消す
//   ・1 分ごとに中身を見に行き、変わっていれば描き直す（絞り込みは保ったまま）
(() => {
  const state = { tab: 'all', project: '', query: '' }
  let cursor = -1

  const rows = () => Array.from(document.querySelectorAll('.rows .row'))
  // 折りたたんだ「完了」の中は画面に出ていない。カーソルの行き先にもしない
  const folded = (row) => {
    const done = row.closest('details.done')
    return !!done && !done.open
  }
  const shownRows = () => rows().filter((r) => !r.hidden && !folded(r))

  const matches = (row) => {
    if (state.project && row.dataset.project !== state.project) return false
    if (state.query && !row.dataset.text.includes(state.query)) return false
    if (state.tab === 'open') return row.classList.contains('is-open')
    if (state.tab === 'new') return row.classList.contains('is-new')
    return true
  }

  const apply = () => {
    let shown = 0
    for (const row of rows()) {
      const ok = matches(row)
      row.hidden = !ok
      if (ok && !row.dataset.done) shown += 1
    }
    // 完了は「すべて」のときだけ畳んだまま置いておく
    const done = document.querySelector('details.done')
    if (done) done.hidden = state.tab !== 'all'

    const empty = document.querySelector('.empty')
    if (empty) empty.hidden = shown > 0

    for (const tab of document.querySelectorAll('.tab')) {
      tab.classList.toggle('is-on', tab.dataset.tab === state.tab)
    }
    for (const f of document.querySelectorAll('.f')) {
      f.classList.toggle('is-on', (f.dataset.project || '') === state.project)
    }
    moveCursor(0, true)
  }

  // reset は絞り込み直後の呼び出し。行が減ってはみ出したときだけ位置を戻し、
  // まだ一度も J / K を押していない状態（-1）はそのまま保つ
  const moveCursor = (delta, reset) => {
    const list = shownRows()
    for (const r of rows()) r.classList.remove('is-cursor')
    if (!list.length) { cursor = -1; return }
    if (reset) {
      if (cursor >= list.length) cursor = list.length - 1
      if (cursor < 0) return
    } else {
      cursor = Math.min(Math.max(cursor + delta, 0), list.length - 1)
    }
    list[cursor].classList.add('is-cursor')
    list[cursor].scrollIntoView({ block: 'nearest' })
  }

  const bind = () => {
    for (const tab of document.querySelectorAll('.tab')) {
      tab.addEventListener('click', () => { state.tab = tab.dataset.tab; apply() })
    }
    for (const f of document.querySelectorAll('.f')) {
      f.addEventListener('click', () => {
        const next = f.dataset.project || ''
        state.project = state.project === next ? '' : next
        apply()
      })
    }
    const done = document.querySelector('details.done')
    if (done) done.addEventListener('toggle', () => moveCursor(0, true))
    const q = document.querySelector('.q')
    if (q) {
      q.value = state.query
      q.addEventListener('input', () => { state.query = q.value.trim().toLowerCase(); apply() })
      q.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { q.value = ''; state.query = ''; apply(); q.blur() }
      })
    }
  }

  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')
    if (e.key === '/' && !typing) { e.preventDefault(); document.querySelector('.q')?.focus(); return }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return
    if (e.key === 'j') { e.preventDefault(); moveCursor(1, false) }
    else if (e.key === 'k') { e.preventDefault(); moveCursor(-1, false) }
    else if (e.key === 'Enter') {
      const row = shownRows()[cursor]
      if (row) { e.preventDefault(); row.querySelector('a')?.click() }
    }
  })

  // ---- 中身が変わったら描き直す（開きっぱなしでも画面が跳ねない） -------------
  let signature = document.body.dataset.signature
  const check = async () => {
    try {
      const res = await fetch('/api/signature', { cache: 'no-store' })
      if (!res.ok) return
      const data = await res.json()
      if (data.signature === signature) return
      const page = await (await fetch('/', { cache: 'no-store' })).text()
      const fresh = new DOMParser().parseFromString(page, 'text/html')
      const wrap = fresh.querySelector('.wrap')
      if (!wrap) return
      const doneOpen = document.querySelector('details.done')?.open
      document.querySelector('.wrap').replaceWith(wrap)
      const done = document.querySelector('details.done')
      if (done && doneOpen) done.open = true
      signature = fresh.body.dataset.signature || data.signature
      document.body.dataset.signature = signature
      bind()
      apply()
    } catch {
      // サーバーが止まっているだけ。次の周期でまた見に行く
    }
  }
  setInterval(check, 60000)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void check()
  })

  bind()
  apply()
})()
