//填写相关参数打开70games网页，进入游览器开发者模式，在控制台粘贴此代码回车运行

//填入Cookie
const Cookie = ''
//复制Cookie里的sid
const sid =''
//开始页
const start = 1
//结束页
const end = 100

function sendGetRequest(url) {
  return new Promise((resolve, reject) => {
    fetch(`https://70games.net/${url}`, {
      method: 'GET',
      headers: {
        'Host': '70games.net',
        'Connection': 'keep-alive',
        'Cache-Control': 'max-age=0',
        'sec-ch-ua': '"Microsoft Edge";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-User': '?1',
        'Sec-Fetch-Dest': 'document',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
        'Cookie': Cookie
      }
    })
    .then(response => {
      if (!response.ok) {
        throw new Error('Network response was not ok');
      }
      return response.text();
    })
    .then(data => resolve(data))
    .catch(error => {
      console.error(error);
      reject(null);
    });
  });
}
const strings = [];
const parser = new DOMParser();
async function getData() {
  try {
    for (let i = start; i < end; i++) {
    var hrefValues = [];
    const data = await sendGetRequest(`forum-1-${i}.htm?digest=2`);
    const elements = parser.parseFromString(data, 'text/html').querySelectorAll('a.post_title');
    for (let i = 0; i < elements.length; i++) {
      hrefValues.push(elements[i].getAttribute('href'));
    }
    for (let i = 0; i < hrefValues.length; i++) {
    const firstNumber = parseInt(hrefValues[i].match(/\d+/)[0]);
    $.xpost(`post-create-${firstNumber}-1.htm`, `doctype=1&return_html=1&quotepid=0&sid=${sid}&message=2%0D%0A`,function(code, message) {}, undefined);
      var Values = [];
      const data = await sendGetRequest(hrefValues[i]);
      const elements = parser.parseFromString(data, 'text/html').querySelectorAll('.coded.col');
      elements.forEach(element => {
      Values.push(element.getAttribute('value'));
      });
        if (Values.length==2) {
            strings.push(`账号 ${Values[0]} 密码 ${Values[1]}`);
            console.log(`账号 ${Values[0]} 密码 ${Values[1]}`);
        }
    }
   }
    //导出到文件
const blob = new Blob([strings.join('\n')], { type: 'text/plain' });
const url = URL.createObjectURL(blob);
const link = document.createElement('a');
link.href = url;
link.download = 'example.txt';
document.body.appendChild(link);
link.click();
URL.revokeObjectURL(url);
  } catch (error) {
    console.error(error);
  }
}
getData();
