(function(){
var F=window.fetch;
function hex(x){var a=new Uint8Array(x),s='';for(var i=0;i<a.length;i++)s+=a[i].toString(16).padStart(2,'0');return s}
function out(s){
 try{navigator.clipboard.writeText(s);alert('コピーしました！\nDiscordに貼ってください。\n\n注文は送っていません（この後エラーが出ますが正常です）');return}catch(e){}
 var t=document.createElement('textarea');t.value=s;
 t.style.cssText='position:fixed;top:10%;left:5%;width:90%;height:40%;z-index:2147483647;font:11px monospace';
 document.body.appendChild(t);t.select();
 alert('長押しして全部コピーしてください');
}
window.fetch=function(i,n){
 var u=(i&&i.url)||String(i);
 if(!/CheckoutOrder|StoreOrder/.test(u))return F.apply(this,arguments);
 var b=n&&n.body,p=null;
 if(b instanceof ArrayBuffer)p=Promise.resolve(b);
 else if(ArrayBuffer.isView(b))p=Promise.resolve(b.buffer);
 else if(i&&i.clone)p=i.clone().arrayBuffer();
 if(p)p.then(function(x){if(x&&x.byteLength)out(hex(x))});
 return new Response('',{status:499});
};
alert('準備できました。\nこのまま支払いへ進んでください。\n注文は送信されません。');
})();
