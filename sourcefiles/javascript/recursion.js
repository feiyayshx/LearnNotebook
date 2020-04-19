function factorial(n) {
    debugger
    if (n === 1) return 1
    return n * factorial(n - 1)
  }
  var v = factorial(5)
  console.log(v)