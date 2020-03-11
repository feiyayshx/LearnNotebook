/* 同步任务-》本轮循环-》次轮循环，process.nextTick 队列再同步任务执行完后立即执行，promise.then回调在微任务队列，追加在process.nextTick队列后面 */

console.log(
  '<--------------Node环境浏览器执行机制-------------->',
) /* result： 1，7，6，8，2，4，3，5，9，11，10，12*/
/*==================js执行机制案例1====================*/
/**
 * @return:
 * timer1,timer2,promise1,promise2
 * */

/* setTimeout(() => {
  console.log('timer1')

  Promise.resolve().then(function() {
    console.log('promise1')
  })
}, 0)

setTimeout(() => {
  console.log('timer2')

  Promise.resolve().then(function() {
    console.log('promise2')
  })
}, 0) */

/*  ====================js 执行机制 案例2====================  */
/**
 * @return:
 * script start, async1 start,async2,promise1,script end,process,promise2,async1 end,setTimeout, setImmediate
 *  */

/* async function async1() {
  console.log('async1 start')
  await async2()
  console.log('async1 end')
}
async function async2() {
  console.log('async2')
}
console.log('script start')
setTimeout(function() {
  console.log('setTimeout')
})
async1()
new Promise(function(resolve) {
  console.log('promise1')
  resolve()
}).then(function() {
  console.log('promise2')
})
setImmediate(() => {
  console.log('setImmediate')
})
process.nextTick(() => {
  console.log('process')
})
console.log('script end')
 */

/*  ===================js 执行机制 案例3=====================  */

/**
 * @return:
 * script start, async1 start,async2,promise1,promise2,script end,nextTick,promise3,async1 end,setTimeout0,setTimeout3,setImmediate
 *  */
/* async function async1() {
  console.log('async1 start')
  await async2()
  console.log('async1 end')
}
async function async2() {
  console.log('async2')
}
console.log('script start')
setTimeout(function() {
  console.log('setTimeout0')
}, 0)
setTimeout(function() {
  console.log('setTimeout3')
}, 3)
setImmediate(() => console.log('setImmediate'))
process.nextTick(() => console.log('nextTick'))
async1()
new Promise(function(resolve) {
  console.log('promise1')
  resolve()
  console.log('promise2')
}).then(function() {
  console.log('promise3')
})
console.log('script end') */

/*  ========================js 执行机制 案例4=========================  */ console.log('1')
setTimeout(function() {
  console.log('2')
  setTimeout(() => { console.log('13') }, 20)
  process.nextTick(function() {
    console.log('3')
  })
  new Promise(function(resolve) {
    console.log('4')
    resolve()
  }).then(function() {
    console.log('5')
  })
})
process.nextTick(function() {
  console.log('6')
})
setImmediate(() => console.log('setImmediate'))
new Promise(function(resolve) {
  console.log('7')
  resolve()
}).then(function() {
  console.log('8')
})

setTimeout(function() {
  console.log('9')
  process.nextTick(function() {
    console.log('10')
  })
  new Promise(function(resolve) {
    console.log('11')
    resolve()
  }).then(function() {
    console.log('12')
  })
})

/* setTimeout(() => {
  //settimeout1
  console.log('1')
  new Promise(resolve => {
    console.log('2')
    resolve()
  }) // Promise3
    .then(() => {
      console.log('3')
    })
  new Promise(resolve => {
    console.log('4')
    resolve()
  }) // Promise4
    .then(() => {
      console.log('5')
    })
  setTimeout(() => {
    // settimeout3
    console.log('6')
    setTimeout(() => {
      // settimeout5
      console.log('7')
      new Promise(resolve => {
        console.log('8')
        resolve()
      }) // Promise5
        .then(() => {
          console.log('9')
        })
      new Promise(resolve => {
        console.log('10')
        resolve()
      }) // Promise6
        .then(() => {
          console.log('11')
        })
    })
    setTimeout(() => {
      console.log('12')
    }, 0) // settimeout6
  })
  setTimeout(() => {
    console.log('13')
  }, 0) // settimeout4
})
setTimeout(() => {
  console.log('14')
}, 0) // settimeout2
new Promise(resolve => {
  console.log('15')
  resolve()
}) // Promise1
  .then(() => {
    console.log('16')
  })
new Promise(resolve => {
  console.log('17')
  resolve()
}) // Promise2
  .then(() => {
    console.log('18')
  })
 */