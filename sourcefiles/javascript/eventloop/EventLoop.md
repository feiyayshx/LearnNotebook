## JavaScript的执行机制-事件循环(EventLoop)

### 一. 关于JavaScript

#### JavaScript 是一门单线程非阻塞的脚本语言，单线程是由执行环境-浏览器决定的，即JS只有一个主线程；因为JS需要与浏览器进行交互，操作dom，若是多线程同时操作同一个dom会引发状态同步等问题，故单线程是必须的。

#### 虽然H5开放了Web Worker，为JS提供了多线程的环境，但是WebWorker并不能操作DOM对象，而且受到主线程的控制，不能独立执行，本质上JS依然是单线程。
#### 另外JS有非阻塞的特点，即遇到异步任务，不会阻碍后面代码的执行，主线程会继续向下执行；而实现这一特点的方法就是事件循环，也就是JS的执行机制。JS所在的宿主环境不同，其执行机制也不相同，本文主要探讨JS在浏览器及Node环境中的执行机制。

- 注意: JS的运行机制由JS解析引擎决定的，在不同的宿主环境下都是统一的。有兴趣的可以自行学习
  
### 二. 浏览器环境中的事件循环

#### 1. 执行栈和事件队列

#### 当js脚本第一次加载时，js引擎会解析这段代码并创建全局执行上下文，压入执行栈，根据执行栈FILO的特性，js主线程每次从栈顶取代码执行，当调用函数时，就为该函数创建一个自己的函数执行上下文，并压入栈的顶部。
#### 执行上下文(Execution Context)是什么呢? 就是js代码被解析和执行时所在的环境。一个程序中只能存在一个全局执行上下文，可以有多个函数执行上下文，因为每次函数调用时都会创建一个函数执行上下文。而执行栈就是管理执行上下文的栈结构，遵循先进后出(FILO)的原则。

#### 从广义上说，js代码分同步和异步。
#### （1）js对于同步代码的执行：js主线程从执行栈中先依次执行同步代码。如果当前执行的是一个函数，js会向执行栈中添加这个函数的执行上下文，并进入到这个执行上下文继续执行其中的代码，执行完毕后返回结果，js退出这个执行上下文并把它销毁，回到上一个执行上下文继续执行其代码，该过程重复进行，直到执行栈中的代码全部执行完毕。图例如下：
![](../images/context.gif)

#### （2）js对于异步代码的执行：当js引擎遇到一个异步事件后并不会一直等待其返回结果，而是会将这个事件挂起，继续执行执行栈中的其他任务。当这个异步事件返回结果后，js会将这个事件的回调加入到一个队列，我们称之为事件队列。被放入事件队列不会立刻执行其回调，而是等待当前执行栈中的所有任务都执行完毕， 主线程处于闲置状态时，主线程会去查找事件队列是否有任务。如果有，那么主线程会从中取出排在第一位的事件，并把这个事件对应的回调放入执行栈中，然后执行其中的同步代码...，如此反复，这样就形成了一个无限的循环。这个过程被称为“事件循环（Event Loop）”。
- 事件队列又分为：宏任务队列(macro-task queue)和微任务队列(micro-task queue)
  
- macro-task(宏任务)：包括整体代码script，setTimeout，setInterval，MessageChannel，postMessage、setImmediate
- micro-task(微任务)：Promise.then()，process.nextTick，MutationObsever
 #### 不同类型的任务会进入到对应的事件队列中。其事件循环流程为：
 - js初始进入执行栈执行代码，开始第一轮事件循环；
 - 当执行栈为空时，js查询微任务队列，如果有任务，则取出第一个任务的回调放入执行栈，然后执行，直到微任务队列为空，第一轮事件循环结束；
 - 当微任务队列为空时，js查询宏任务队列，第二轮事件循环开始，如果有任务，则取出第一个任务的回调放入执行栈，然后执行，直到执行栈为空，js再去查询微任务队列是否有任务，如果有则继续取出放入执行栈执行，直到微任务队列为空，则第二轮事件循环结束，以此重复进行... 直到程序执行完毕。
#### 注意： 同一次事件循环中，微任务永远在宏任务之前执行

3. 案例分析：
   ```
    console.log('1')
    setTimeout(function() {  // setTimeout1
    console.log('2')
    new Promise(function(resolve) {  // promise1
        console.log('4')
        resolve()
    }).then(function() {  // then1
        console.log('5')
    })
    })
    new Promise(function(resolve) {  // promise2
    console.log('7')
    resolve()
    }).then(function() { // then2
    console.log('8')
    })

    setTimeout(function() {  // setTimeout2
    console.log('9')
    new Promise(function(resolve) { // promise3
        console.log('11')
        resolve()
    }).then(function() {  // then3
        console.log('12')
    })
    })
   ```
 - step1：从执行栈中执行代码，第一次事件循环开始，先执行同步代码，输出1,7；遇到promise回调立即执，是同步代码；遇到setTimeout异步任务时，将该异步结果回调放入宏任务队列，即macro-task queue为: setTimeout1,setTimeout2；遇到promise.then()回调时，将回调放入微任务队列，即micro-task queue为: then2;
 - step2: 同步代码执行完毕，输出1，7；此时执行栈为空，则检查微任务队列是否为空，不为空则从队列中取出一个任务放到执行栈中执行，输出8；此时执行栈为空，查询微任务队列也是空，则第一次事件循环结束。
 - step3: 微任务队列为空，然后查询宏任务队列，开始第二次事件循环，从队列中取出setTimeout1放到执行栈中执行，输出2，4，遇到then1,放入微任务队列,micro-mask queue为： then1;此时执行栈为空，检查微任务队列，取出任务放入执行栈中执行，输出5，然后微任务队列为空，第二次循环结束；
 - step4:检查宏任务队列，第三次循环开始，将setTimeout2放入执行栈执行，输出：9，11，然后将then3放入微任务队列，执行栈为空，继续从微任务队列取任务执行，输出12；此时第三次循环结束；最后的输出结果为：1，7，8，2，4，5，9，11，12
  
  ```
     async function async1() {
        console.log('async1 start')
        await async2() // promise
        console.log('async1 end') // async1 then
      }
      async function async2() {
        console.log('async2')
      }
      console.log('script start')
      setTimeout(function() {  // setTimeout1
        console.log('setTimeout1')
      }, 0)
      setTimeout(function() {  // setTimeout2
        console.log('setTimeout2')
      }, 3)
      async1()
      new Promise(function(resolve) { 
        console.log('promise1')
        resolve()
        console.log('promise2')
      }).then(function() {  // then1
        console.log('promise3')
      })
      console.log('script end')
  ```
  - step1: 从执行栈中执行代码，遇到同步代码直接输出script start，async1 start，async2，promise1，promise2，script end；macro-task queue: [setTimeout1,setTimeout2]
  micro-task queue: [async1then,then1]
  - step2: 执行栈为空，从微任务队列取任务放到执行栈执行，直到微任务队列为空，输出async1 end,promise3
  - step3: 微任务队列为空，检查宏任务队列并压入执行栈中执行，依次输出setTimeout1,setTimeout2；最后的输出结果为：script start，async1 start，async2，promise1，promise2，script end,async1 end,promise3,setTimeout1,setTimeout2
  
### 三. Node环境中的事件循环

#### Node.js引入chrome v8引擎作为js解释器，而Node中处理I/O请求，实现异步编程的则由libuv引擎完成，其事件循环也是由libuv引擎实现的。Node中的事件循环有六个阶段，官方图例如下：

![](../images/event-loop1.png)
### 各阶段概述
#### timer：本阶段执行已经被 setTimeout() 和 setInterval() 调度的回调函数。
#### pending callback：执行延迟到下一个循环迭代的 I/O 回调。
#### idle, prepare：仅系统内部使用。
#### poll：检索新的 I/O 事件;执行与 I/O 相关的回调（几乎所有情况下，除了关闭的回调函数，那些由计时器和 setImmediate() 调度的之外），其余情况 node 将在适当的时候在此阻塞。
#### check：setImmediate() 回调函数在这里执行。
#### close callback：一些关闭的回调函数，如：socket.on('close', ...)。

### 关键阶段的详细描述
#### 1. poll阶段(轮询阶段)： 当 Node.js 启动后，它会初始化事件轮询；处理已提供的输入脚本，它可能会调用一些异步的 API、调度定时器，或者调用 process.nextTick()，然后开始处理事件循环。

#### poll阶段两个重要功能：(1)计算应该阻塞和轮询I/O的时间;(2)处理轮询队列里的事件；

#### poll阶段的执行逻辑：（1）当事件循环进入poll阶段且没有被调度的计时器，如果poll队列不为空，则依次执行队列里的任务，直到队列为空；当队列为空时，检查是否有setImmediate()回调,若有，则进入check阶段执行队列中的任务，若没有，则事件循环将等待回调被添加到队列中，然后立即执行。

#### （2）poll 队列为空时，事件循环将检查已到达时间阈值的定时器，如果有被调度的计时器，则事件循环将绕回timer阶段以执行这些计时器的回调

#### 2. timer阶段：
#### 在指定的一段时间间隔后，setTimeout()和setInternal()回调将被尽可能早地运行。但是，操作系统调度或其它正在运行的回调可能会延迟它们。

#### 注意：poll阶段控制何时定时器执行。

#### 3. check阶段：
#### 通常，在执行代码时，事件循环最终会命中poll阶段，在那等待传入连接、请求等，如果poll阶段队列变为空，并且有setImmediate()回调，则事件循环继续到check阶段而不是等待。

#### 4. close callback
#### 如果套接字或处理函数突然关闭（例如 socket.destroy()），则'close' 事件将在这个阶段发出。否则它将通过 process.nextTick() 发出。

#### 5. setImmeidiate()和setTimeout()对比

### node执行js代码及事件循环过程
 1、node初始化

- 初始化node环境
- 执行输入的代码
- 执行process.nextTick回调
- 执行微任务(microtasks)
  
2、进入事件循环

 2.1、进入Timer阶段

- 检查Timer队列是否有到期的Timer的回调，如果有，将到期的所有Timer回调按照TimerId升序执行
- 检查是否有process.nextTick任务，如果有，全部执行
- 检查是否有微任务(promise)，如果有，全部执行
- 退出该阶段
  
2.2、进入Pending I/O Callback阶段

- 检查是否有Pending I/O Callback的回调，如果有，执行回调。如果没有退出该阶段
- 检查是否有process.nextTick任务，如果有，全部执行
- 检查是否有微任务(promise)，如果有，全部执行
- 退出该阶段
  
2.3、进入idle，prepare阶段

- 这个阶段与JavaScript关系不大，略过

2.4、进入Poll阶段

- 首先检查是否存在尚未完成的回调，如果存在，分如下两种情况：

  - 第一种情况：有可执行的回调

    - 执行所有可用回调(包含到期的定时器还有一些IO事件等)
    - 检查是否有process.nextTick任务，如果有，全部执行
    - 检查是否有微任务(promise)，如果有，全部执行
    - 退出该阶段
  
   - 第二种情况：没有可执行的回调

     - 检查是否有immediate回调，如果有，退出Poll阶段。如果没有，阻塞在此阶段，等待新的事件通知
- 如果不存在尚未完成的回调，退出Poll阶段

2.5、进入check阶段

- 如果有immediate回调，则执行所有immediate回调
- 检查是否有process.nextTick任务，如果有，全部执行
- 检查是否有微任务(promise)，如果有，全部执行
- 退出该阶段
  
2.6、进入closing阶段

- 如果有immediate回调，则执行所有immediate回调
- 检查是否有process.nextTick任务，如果有，全部执行
- 检查是否有微任务(promise)，如果有，全部执行
- 退出该阶段
  
3、检查是否有活跃的handles(定时器、IO等事件句柄)

- 如果有，继续下一轮事件循环
- 如果没有，结束事件循环，退出程序
  

注意：事件循环的每一个子阶段退出之前都会按顺序执行如下过程：

- 检查是否有 process.nextTick 回调，如果有，全部执行。
- 检查是否有 微任务(promise)，如果有，全部执行。

```
console.log('1')
setTimeout(function() { // setTimeout1
  console.log('2')
  setTimeout(() => { console.log('13') }, 2)  // setTimeout3
  process.nextTick(function() { // nextTick2
    console.log('3')
  })
  new Promise(function(resolve) { 
    console.log('4')
    resolve()
  }).then(function() { // promise2
    console.log('5')
  })
})
process.nextTick(function() {  // nextTick1
  console.log('6')
})
setImmediate(() => console.log('setImmediate'))
new Promise(function(resolve) {  
  console.log('7')
  resolve()
}).then(function() { // promise1
  console.log('8')
})

setTimeout(function() { // setTimeout2
  console.log('9')
  process.nextTick(function() {  // nextTick3
    console.log('10')
  })
  new Promise(function(resolve) {
    console.log('11')
    resolve()
  }).then(function() {  // promise3
    console.log('12')
  })
})

```
- node初始化:
  - 执行输入的js代码
    - 遇到setTimeout,将回调推入timer队列，记为setTimeout1
    - 遇到process.nextTick，将回调推入process.nextTick队列，记为：nextTick1
    - - 遇到setImmediate,将回调推入check队列，记为：immediate1
    - 遇到promise，执行，输出7，将then()回调推入微任务队列，记为：promise1
    - 遇到setTimeout,将回调推入timer队列，记为setTimeout2
    - 代码执行结束，输出1，7
  - 检查有process.nextTick()任务，执行，输出6
  - 执行微任务
    - 检查微任务队列，存在回调：promise1
    - 执行promise1回调，输出：8
- 第一次事件循环：
  - 进入timer阶段
    - 检查timer队列，存在回调：setTimeout1,setTimeout1
    - 执行setTimeout1，输出2,4,微任务队列：promise2;process.nextTick队列有：nextTick2;添加timer任务，记为：setTimeout3
    - 执行setTimeout2，输出9，11，微任务队列为：promise2,promise3,nextTick队列有：nextTick2,nextTick3
    - timer队列执行完毕，检查process.nextTick,执行，输出3，10
    - 检查微任务队列，执行，输出5，12
    - 退出timer阶段
  - Pending Callback阶段没有任务，略过
  - 进入poll阶段
    - 检查是否存在没有完成的回调，此时poll队列为空，存在setImmediate回调，则进入check阶段
  - 进入check阶段
    - check队列不为空，执行，输出setImmediate 
  - closing阶段没有任务，略过
  - 检查是否还有活跃的handles(定时器、IO等事件句柄),有，继续下一轮事件循环
- 第二次事件循环：
  - 进入timer阶段：
    - 检查timer队列，有回调：setTimeout3，执行，输出13
    - process.nextTick没有，忽略
    - 微任务没有，忽略
    - 退出timer阶段
  - Pending Callback、Poll、check、closing阶段没有任务，略过
  - 检查是否还有活跃的handles(定时器、IO等事件句柄),没有了，结束事件循环，退出程序
- 程序最后输出结果：1，7，6，8，2，4，9，11，3，10，5，12，setImmediate，13
#### 引用参考文章：

- [详解JavaScript中的Event Loop（事件循环）机制](https://zhuanlan.zhihu.com/p/33058983)

- [Node.js 事件循环](https://nodejs.org/zh-cn/docs/guides/event-loop-timers-and-nexttick/)
  
- [JS代码在nodejs环境下执行机制和事件循环](https://segmentfault.com/a/1190000018730085)