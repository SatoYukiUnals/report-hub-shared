// スライド様式のレポートを 1 枚ずつ送る。
//
// 使い方（レポート HTML 側）：<section class="slide"> を並べるだけでよい。
// 送りのボタン・進捗・一覧はこのスクリプトが差し込む。
//
//   <link rel="stylesheet" href="/assets/slide.css">
//   <section class="slide cover"> … </section>
//   <section class="slide" data-title="一覧に出す短い題"> … </section>
//   <script src="/assets/slide.js"></script>
//   <script src="/assets/answers.js"></script>   ← 確認事項があるときだけ
//
// 中身は静的な HTML のまま書く（このスクリプトは表示の切り替えしかしない）。
// サーバーの一覧は本文の data-qa-id と class="eyebrow" を文字列として読むので、
// 設問や種別を JS で組み立ててはいけない。
//
// 操作：→ / ␣ / j で次、← / k で前、Home / End で端、o で一覧、p で連続表示、
// Esc で一覧を閉じる。現在位置は URL の #s3 に入るので、開き直しても戻ってくる。
// ── mermaid（時系列・分岐のある図） ──────────────────
// <pre class="mermaid">…</pre> で書いた図だけを描く。1 つも無ければ
// mermaid.min.js（3.5MB）自体を読み込まない。描画は非同期で行い、
// 枚の送り（上の IIFE）とは独立して進む。失敗しても元のテキスト
// （pre.mermaid のまま）が残るので、JS が動かない・描画に失敗した
// 場合でも図の元になった記法が読める。
const renderMermaidDiagrams = () => {
  const blocks = Array.from(document.querySelectorAll('pre.mermaid'))
  if (!blocks.length) return

  const loadScript = (src) => new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = src
    s.onload = () => resolve()
    s.onerror = () => reject(new Error(`failed to load ${src}`))
    document.head.appendChild(s)
  })

  loadScript('/assets/mermaid.min.js').then(async () => {
    const mermaid = window.mermaid
    if (!mermaid) return
    // 配色はレポートの紙面（tokens.css）に合わせる。ダークモードは無い
    // （report.css 同様、読み物として常に同じ見え方にする）
    //
    // 注意：mermaid が生成する SVG は自分のスタイルを内部に焼き込むため、
    // ここの色は tokens.css の CSS 変数を参照できず、16進値を直接書いている
    // （themeVariables に var(--ink) 等を渡しても解決されない）。
    // tokens.css の配色を変えたときは、対応する値をここにも手で反映すること。
    // 片方だけ直すと紙面の色と図の色が食い違う。
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      fontFamily: 'system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif',
      theme: 'base',
      themeVariables: {
        background: '#fffdf4',
        primaryColor: '#e2eef5',
        primaryTextColor: '#2a2016',
        primaryBorderColor: '#2f6b8f',
        secondaryColor: '#f6efdd',
        tertiaryColor: '#fffdf4',
        lineColor: '#6d5b43',
        textColor: '#2a2016',
        mainBkg: '#e2eef5',
        nodeBorder: '#2f6b8f',
        clusterBkg: '#f6efdd',
        clusterBorder: '#d3ba95',
        edgeLabelBackground: '#fffdf4',
        actorBkg: '#e2eef5',
        actorBorder: '#2f6b8f',
        actorTextColor: '#2a2016',
        signalColor: '#6d5b43',
        signalTextColor: '#2a2016',
      },
    })

    // 枚が display:none でも mermaid.render は自前のオフスクリーン領域で
    // 計算するため、いま表示中の枚かどうかに関わらず全図をここで描く。
    for (let i = 0; i < blocks.length; i += 1) {
      const block = blocks[i]
      const code = block.textContent
      try {
        const { svg } = await mermaid.render(`mermaid-diagram-${i}`, code)
        const wrap = document.createElement('div')
        wrap.className = 'mermaid-rendered'
        wrap.innerHTML = svg
        block.replaceWith(wrap)
      } catch (err) {
        // 静かに諦める。block はそのまま残るので元のテキストが読める
        console.debug('mermaid render failed', err)
      }
    }
  }).catch((err) => {
    console.debug('mermaid load failed', err)
  })
}

renderMermaidDiagrams()

;(() => {
  const slides = Array.from(document.querySelectorAll('.slide'))
  if (slides.length < 2) return

  const root = document.documentElement
  root.classList.add('deck-on')

  const titleOf = (slide, i) => {
    if (slide.dataset.title) return slide.dataset.title
    const head = slide.querySelector('h1, h2, h3')
    const text = head ? head.textContent.trim().replace(/\s+/g, ' ') : ''
    return text || `${i + 1} 枚目`
  }

  const kindOf = (slide) => {
    if (slide.classList.contains('cover')) return '表紙'
    if (slide.classList.contains('chapter')) return '章'
    // 設問は本編の枚にも置く。何問あるかまで出して、一覧から答えに行けるようにする
    const asks = slide.querySelectorAll('.qa[data-qa-id]').length
    if (asks) return asks > 1 ? `設問 ${asks}` : '設問'
    return ''
  }

  // ── 差し込む部品 ──────────────────────────────────
  const progressTrack = document.createElement('div')
  progressTrack.className = 'deck-progress-track'
  const progress = document.createElement('div')
  progress.className = 'deck-progress'

  const nav = document.createElement('nav')
  nav.className = 'deck-nav'
  const prev = document.createElement('button')
  prev.type = 'button'; prev.textContent = '‹'; prev.title = '前へ（←）'
  const count = document.createElement('span')
  count.className = 'deck-count'
  const next = document.createElement('button')
  next.type = 'button'; next.textContent = '›'; next.title = '次へ（→）'
  const mapButton = document.createElement('button')
  mapButton.type = 'button'; mapButton.textContent = '⊞'; mapButton.title = '一覧（o）'
  nav.append(prev, count, next, mapButton)

  const hint = document.createElement('p')
  hint.className = 'deck-hint'
  hint.textContent = '← → で送る ／ o で一覧 ／ p で通し読み'

  document.body.append(progressTrack, progress, nav, hint)
  setTimeout(() => hint.classList.add('is-faded'), 4200)

  // ── 一覧 ──────────────────────────────────────────
  const map = document.createElement('div')
  map.className = 'deck-map'
  map.hidden = true
  const mapInner = document.createElement('div')
  mapInner.className = 'deck-map-inner'
  const mapHead = document.createElement('h2')
  mapHead.textContent = '全体（クリックでその枚へ）'
  const mapList = document.createElement('ol')
  mapInner.append(mapHead, mapList)
  map.append(mapInner)
  document.body.append(map)

  const mapButtons = slides.map((slide, i) => {
    const li = document.createElement('li')
    const b = document.createElement('button')
    b.type = 'button'
    const n = document.createElement('span'); n.className = 'n'; n.textContent = String(i + 1)
    const t = document.createElement('span'); t.textContent = titleOf(slide, i)
    b.append(n, t)
    const kind = kindOf(slide)
    if (kind) { const k = document.createElement('span'); k.className = 'k'; k.textContent = kind; b.append(k) }
    b.addEventListener('click', () => { closeMap(); go(i) })
    li.append(b)
    mapList.append(li)
    return b
  })

  const openMap = () => { map.hidden = false; mapButtons[at]?.focus() }
  const closeMap = () => { map.hidden = true }
  const toggleMap = () => (map.hidden ? openMap() : closeMap())

  // ── 送り ──────────────────────────────────────────
  let at = 0

  const paint = () => {
    slides.forEach((slide, i) => slide.classList.toggle('is-current', i === at))
    mapButtons.forEach((b, i) => b.classList.toggle('is-current', i === at))
    count.replaceChildren()
    const now = document.createElement('span')
    now.className = 'now'; now.textContent = String(at + 1)
    const of = document.createElement('span')
    of.className = 'of'; of.textContent = ` / ${slides.length}`
    count.append(now, of)
    prev.disabled = at === 0
    next.disabled = at === slides.length - 1
    progress.style.width = `${((at + 1) / slides.length) * 100}%`
  }

  const go = (i, { push = true } = {}) => {
    const to = Math.max(0, Math.min(slides.length - 1, i))
    if (to === at && push) return
    at = to
    paint()
    window.scrollTo(0, 0)
    if (push) history.replaceState(null, '', `#s${at + 1}`)
  }

  const fromHash = () => {
    const m = location.hash.match(/^#s(\d+)$/)
    return m ? Number(m[1]) - 1 : 0
  }

  // ── 連続表示（印刷・検索・通し読み用） ─────────────
  const flow = () => {
    const on = root.classList.toggle('deck-flow')
    hint.classList.remove('is-faded')
    hint.textContent = on ? '通し読み（p で 1 枚ずつに戻る）' : '← → で送る ／ o で一覧 ／ p で通し読み'
    setTimeout(() => hint.classList.add('is-faded'), 3200)
    if (!on) go(at, { push: false })
    else slides[at].scrollIntoView({ block: 'start' })
  }

  // ── 入力 ──────────────────────────────────────────
  const typing = (el) => el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable)

  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    if (typing(document.activeElement) && e.key !== 'Escape') return
    if (!map.hidden && e.key !== 'Escape' && e.key !== 'o') return
    switch (e.key) {
      case 'ArrowRight': case ' ': case 'j': case 'PageDown': go(at + 1); break
      case 'ArrowLeft': case 'k': case 'PageUp': go(at - 1); break
      case 'Home': go(0); break
      case 'End': go(slides.length - 1); break
      case 'o': toggleMap(); break
      case 'p': flow(); break
      case 'Escape': closeMap(); return
      default: return
    }
    e.preventDefault()
  })

  prev.addEventListener('click', () => go(at - 1))
  next.addEventListener('click', () => go(at + 1))
  mapButton.addEventListener('click', toggleMap)
  window.addEventListener('hashchange', () => go(fromHash(), { push: false }))

  // 触って横に払う（スマホ）。縦の指運びは本文のスクロールなので拾わない。
  let x0 = null, y0 = null
  document.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY
  }, { passive: true })
  document.addEventListener('touchend', (e) => {
    if (x0 === null) return
    const dx = e.changedTouches[0].clientX - x0
    const dy = e.changedTouches[0].clientY - y0
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.8) go(at + (dx < 0 ? 1 : -1))
    x0 = y0 = null
  }, { passive: true })

  // 未回答が残っているとき、回答バーの件数を押すと最初の未回答の枚へ飛ぶ。
  // 設問が枚をまたいで散っても、答え残しを取りに行けるようにするため。
  document.addEventListener('click', (e) => {
    const target = e.target.closest?.('.qa-bar-count')
    if (!target) return
    const i = slides.findIndex((slide) =>
      Array.from(slide.querySelectorAll('.qa[data-qa-id]')).some(
        (box) => !box.querySelector('input[type="radio"]:checked')))
    if (i >= 0) go(i)
  })

  go(fromHash(), { push: false })
})()
