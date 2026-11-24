'use strict';
(()=>{
 let size=16;
 try{const saved=Number(localStorage.getItem('hal-status-font-size'));if(saved>=10&&saved<=26)size=saved}catch{}
 function apply(next){
  size=Math.max(10,Math.min(26,next));document.documentElement.style.fontSize=size+'px';
  try{localStorage.setItem('hal-status-font-size',String(size))}catch{}
  document.getElementById('statusFontValue').textContent=size+' px';
  document.getElementById('statusFontSmaller').disabled=size<=10;
  document.getElementById('statusFontLarger').disabled=size>=26;
  window.dispatchEvent(new Event('hal-status-font-change'));
 }
 window.HalStatusFont={scale:()=>size/16,size:()=>size};
 document.getElementById('statusFontSmaller').onclick=()=>apply(size-1);
 document.getElementById('statusFontLarger').onclick=()=>apply(size+1);
 document.getElementById('statusFontReset').onclick=()=>apply(16);
 apply(size);
})();
