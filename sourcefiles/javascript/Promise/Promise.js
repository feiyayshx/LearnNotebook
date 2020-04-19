const PENDING = 'pending'
const FULFILLED = 'resolved'
const REJECTED = 'rejected'

class Promise {
  callbacks = [] // 保存then中的回调
  state = PENDING // 初始状态
  value = null // 终值
  constructor(fn) {
    fn(this._resolve.bind(this), this._reject.bind(this))
  }
  then(onFulfilled, onRejected) {
    return new Promise((resolve, reject) => {
      this._handle({
        onFulfilled: onFulfilled || null,
        onRejected: onRejected || null,
        resolve: resolve,
        reject: reject,
      })
    })
  }
  _resolve(value) {
    // 判断value类型
    if (value && (typeof value === 'object' || typeof value === 'function')) {
      // promise.then(onFulfilled, onRejected)接收两个函数参数
      var then = value.then
      if (typeof then === 'function') {
        var fulfilled = this._resolve.bind(this)
        var rejected = this._reject.bind(this)
        then.call(value, fulfilled, rejected)
        return
      }
    }
    this.state = FULFILLED
    this.value = value
    this.callbacks.map(cb => this._handle(cb))
  }
  _reject(value) {
    this.state = REJECTED
    this.value = value
    this.callbacks.map(cb => this._handle(cb))
  }
  _handle(callbackTask) {
    if (this.state === PENDING) {
      this.callbacks.push(callbackTask)
      return
    }
    let cb =
      this.state === FULFILLED
        ? callbackTask.onFulfilled
        : callbackTask.onRejected
    // then中没有传参数
    if (!cb) {
      cb = this.state === FULFILLED ? callbackTask.resolve : callbackTask.reject
      // 将值传递出去
      cb(this.value)
      return
    }
    // then中有参数，获取函数参数返回值,并传递出去
    let xResult = cb(this.value)
    if (this.state === FULFILLED) {
      callbackTask.resolve(xResult)
    } else if (this.state === REJECTED) {
      callbackTask.reject(xResult)
    }
  }
}
