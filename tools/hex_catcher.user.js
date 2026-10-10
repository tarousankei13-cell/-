// ==UserScript==
// @name         注文コード(HEX)の取り出し
// @namespace    mcd-discord-bot
// @version      1.0
// @description  公式のモバイルオーダーで組んだ注文を、送信する直前に捕まえて16進で見せます。注文も支払いも発生しません。
// @match        https://mobileorder.mcdonalds.co.jp/*
// @match        https://*.mcdonalds.co.jp/order/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * これは何か
 * ----------
 * 公式のモバイルオーダーで商品を選び、支払いボタンを押した瞬間に
 * 送られるはずだったデータを**横取りして表示する**だけのものです。
 *
 *   ・送信は止めるので、**注文も支払いも発生しません**
 *   ・自分のブラウザの、自分の通信を見ているだけです
 *   ・出てきた16進を BOT の `/debug learn` に貼ると、
 *     セットの組み立て方（中間ノード）をBOTが覚えます
 *
 * ⚠️ 止めているので、画面にはエラーが出ます。**それが正常**です。
 *    注文は成立していません。
 *
 * 使い方
 * ------
 *   1. Tampermonkey などに登録する
 *   2. 公式のモバイルオーダーで、覚えさせたい商品を普通に組む
 *      （ハッピーセットなど、BOTで注文できないものが有効）
 *   3. 支払いへ進む
 *   4. 出てきた箱の「コピー」を押す
 *   5. Discord で  /debug learn code:<貼り付け>
 */
(function () {
  'use strict';

  // 止める対象。注文を送る呼び出しだけに絞る。
  // ⚠️ メニューの取得など他の通信まで止めないこと。画面が壊れる。
  var TARGETS = /(CheckoutOrder|StoreOrder)/;

  function toHex(buf) {
    var b = new Uint8Array(buf), out = '';
    for (var i = 0; i < b.length; i++) out += b[i].toString(16).padStart(2, '0');
    return out;
  }

  function show(hex) {
    var old = document.getElementById('hexcatch');
    if (old) old.remove();
    var box = document.createElement('div');
    box.id = 'hexcatch';
    box.style.cssText =
      'position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.6);' +
      'display:flex;align-items:center;justify-content:center;font-family:sans-serif';
    box.innerHTML =
      '<div style="background:#fff;padding:20px;border-radius:10px;width:92%;max-width:540px">' +
      '<h2 style="margin:0 0 6px;font-size:17px">注文コードを取り出しました</h2>' +
      '<p style="margin:0 0 12px;font-size:13px;color:#555">' +
      '送信は止めました。<b>注文も支払いも発生していません。</b><br>' +
      'この下の画面にエラーが出ますが、それで正常です。</p>' +
      '<textarea id="hexcatch-t" readonly style="width:100%;height:120px;padding:8px;' +
      'box-sizing:border-box;border:1px solid #ccc;border-radius:4px;' +
      'font-family:monospace;font-size:11px"></textarea>' +
      '<p style="margin:10px 0 0;font-size:12px;color:#555">' +
      'Discord で <code>/debug learn code:&lt;貼り付け&gt;</code></p>' +
      '<div style="text-align:right;margin-top:10px">' +
      '<button id="hexcatch-c" style="padding:9px 18px;background:#1a73e8;color:#fff;' +
      'border:none;border-radius:4px;cursor:pointer;margin-right:8px">コピー</button>' +
      '<button id="hexcatch-x" style="padding:9px 18px;background:#eee;border:1px solid #ccc;' +
      'border-radius:4px;cursor:pointer">閉じる</button></div></div>';
    document.body.appendChild(box);
    document.getElementById('hexcatch-t').value = hex;
    document.getElementById('hexcatch-x').onclick = function () { box.remove(); };
    document.getElementById('hexcatch-c').onclick = function () {
      var t = document.getElementById('hexcatch-t');
      t.select();
      if (navigator.clipboard) navigator.clipboard.writeText(t.value);
      else document.execCommand('copy');
      this.textContent = '完了';
    };
  }

  async function bodyOf(input, init) {
    try {
      if (init && init.body) {
        if (init.body instanceof ArrayBuffer) return init.body;
        if (ArrayBuffer.isView(init.body)) return init.body.buffer;
        if (init.body instanceof Blob) return await init.body.arrayBuffer();
      }
      if (input instanceof Request) return await input.clone().arrayBuffer();
    } catch (e) {}
    return null;
  }

  var origFetch = window.fetch;
  window.fetch = async function (input, init) {
    var url = input instanceof Request ? input.url : String(input);
    if (!TARGETS.test(url)) return origFetch.apply(this, arguments);

    var buf = await bodyOf(input, init);
    if (buf && buf.byteLength) show(toHex(buf));

    // ⚠️ **送らない。** ここを通すと本当に注文が入ってしまう。
    //    アプリにはエラーとして返す（画面にエラーが出るのが正常）。
    return new Response('', { status: 499, statusText: 'stopped by hex catcher' });
  };

  // XMLHttpRequest を使う経路も塞いでおく（取りこぼし防止）
  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__hexcatch = TARGETS.test(String(u));
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (this.__hexcatch && body) {
      try {
        var buf = body instanceof ArrayBuffer ? body
                : ArrayBuffer.isView(body) ? body.buffer : null;
        if (buf) { show(toHex(buf)); return; }   // 送らない
      } catch (e) {}
    }
    return origSend.apply(this, arguments);
  };

  console.log('[hex catcher] 準備しました。注文の送信を止めて中身を表示します。');
})();
