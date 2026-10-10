(function(){
var T=/(CheckoutOrder|StoreOrder)/;
function H(b){var a=new Uint8Array(b),s='';for(var i=0;i<a.length;i++)s+=a[i].toString(16).padStart(2,'0');return s}
function S(h){
var d=document.createElement('div');
d.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;padding:16px;font-family:sans-serif';
d.innerHTML='<div style="background:#fff;padding:16px;border-radius:12px;width:100%;max-width:460px"><b style="font-size:16px">注文コードを取り出しました</b><p style="font-size:13px;color:#555;margin:8px 0">送信は止めました。<b>注文も支払いも発生していません。</b>この下にエラーが出ますが正常です。</p><textarea id="_hx" readonly style="width:100%;height:120px;font:11px monospace;padding:8px;box-sizing:border-box;border:1px solid #ccc;border-radius:6px"></textarea><div style="text-align:right;margin-top:10px"><button id="_hc" style="padding:12px 20px;font-size:15px;background:#1a73e8;color:#fff;border:0;border-radius:6px">コピー</button> <button id="_hx2" style="padding:12px 20px;font-size:15px;background:#eee;border:1px solid #ccc;border-radius:6px">閉じる</button></div></div>';
document.body.appendChild(d);
var t=d.querySelector('#_hx');t.value=h;
d.querySelector('#_hx2').onclick=function(){d.remove()};
d.querySelector('#_hc').onclick=function(){t.select();t.setSelectionRange(0,999999);if(navigator.clipboard)navigator.clipboard.writeText(t.value);else document.execCommand('copy');this.textContent='完了'};
}
async function B(i,n){try{if(n&&n.body){if(n.body instanceof ArrayBuffer)return n.body;if(ArrayBuffer.isView(n.body))return n.body.buffer;if(n.body instanceof Blob)return await n.body.arrayBuffer()}if(i instanceof Request)return await i.clone().arrayBuffer()}catch(e){}return null}
var F=window.fetch;
window.fetch=async function(i,n){
var u=i instanceof Request?i.url:String(i);
if(!T.test(u))return F.apply(this,arguments);
var b=await B(i,n);if(b&&b.byteLength)S(H(b));
return new Response('',{status:499});
};
var O=XMLHttpRequest.prototype.open,D=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open=function(m,u){this._h=T.test(String(u));return O.apply(this,arguments)};
XMLHttpRequest.prototype.send=function(b){if(this._h&&b){try{var x=b instanceof ArrayBuffer?b:(ArrayBuffer.isView(b)?b.buffer:null);if(x){S(H(x));return}}catch(e){}}return D.apply(this,arguments)};
alert('準備できました。このまま支払いへ進んでください。注文は送信されません。');
})();
