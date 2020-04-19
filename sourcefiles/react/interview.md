### 1. redux 中间件的原理是什么？ 
- 封装dispatch： action与store之间的桥梁；redux-thunk源码

### 2. 你会把数据统一放到redux中管理，还是共享数据放在redux中管理？
- 把数据统一放到redux中： 因为一个组件中的数据有可能有props,state,redux,数据来源太多，遇到问题定位慢 

### 3. componentWillReceiveProps的调用机制
- props发生改变的时候调用，不包括第一次调用

### 4.react性能优化的最佳实践
- immuable.js 和 PureComponent 结合
```
class Test extends React.PureComponent {
    constructor(props) {
        super(props)
    }
    render() {
        return <div>Hello</div>
    }
}

```
### 5. 虚拟dom是什么？ 为什么虚拟dom会提升代码性能？


### 6. webpack中，是借助loader完成的JSX代码的转化，还是babel?
- babel-preset-react

### 7. 调用setState后，发生了什么？
- setState在构造函数及钩子函数中调用是异步的，在setTimeout中是同步的
```
this.setState((preState) =>({
    num: preState ++
}))
// 第二个参数是个回调，获取更新后的state
this.setState({
  count: this.state.count + 1
}, () => {
  this.setState({
    count: this.state.count + 1
  });
});
```

### 8. refs的作用？在什么场景下使用？
- refs 获取组件的实例；在获取dom元素的属性时使用，width/height

### 9. ref是一个函数，有什么好处

### 10. 高阶组件你是怎么理解的，本质是一个什么东西？
- 接收组件作为参数并返回组件的函数
### 11. 受控组件与非受控组件的区别？

### 12. 函数组件和hooks

### 13. this的指向问题一般怎么解决？

### 14. 函数组件怎么做性能优化？
- 函数组件优点：
  - 没有构造器constructor,没有生命周期,没有状态state,代码简洁，占用内存小，性能更佳；
  - 不需要使用this
  - 可以写成无副作用的纯函数
- 函数组件缺点： 
### 15. 哪个生命周期发送ajax?
- componentDidMount 钩子中发送ajax请求
### 16. ssr 原理是什么？

### 17. redux-saga的设计思想？ 什么是sideEffects?

### 18. react,jquery,vue是否可以共存在一个项目中？
- 可以共存，但是要挂载到不同的dom上，不可相互操作dom；

### 19. 组件是什么？类是什么？类被编译成什么？
- 组件就是一个带有功能的UI界面，通常会把重复的UI界面封装成组件；组件本质上就是一个类，被编译后就是构造函数；
  
### 20. 你是如何跟着社区成长的？
- 科学上网，twitter 订阅官方

### 21. 如何避免ajax数据重新获取
- react-redux 状态管理

### 22. react-router4的核心思想是什么？和router3有什么区别？

### 23. reselect是做什么使用的？
- 类似vue中的computed计算属性，提升代码性能

### 24. react-router的基本原理，hashHistory,browserHistory
- hashHistory 不需要后端配合
- browserHistory 需要后端做配置

### 25. 什么情况下使用异步组件？
- reloadable库，路由懒加载

### xss攻击在react中如何防范？
- dangerouslySetInnerHTML={} 慎用

### 26. immutable.js 与redux的最佳实践



