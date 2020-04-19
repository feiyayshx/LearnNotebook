
## 第一部分：Vue相关总结

https://www.jianshu.com/p/9956dee143e3?utm_campaign=maleskine&utm_content=note&utm_medium=seo_notes&utm_source=recommendation

### 1. Vue数据双向绑定的原理
#### vue2.x数据双向绑定实现的流程大致是：在Vue实例化时，主要做了两件事
- 监听数据：observer(data)
  - 在监听数据的过程中调用Object.defineProperty(obj,key,fn)将data中的每个属性转为访问器属性，并添加一个主体对象dep，用来发布通知和收集订阅者
  
- 编译html: compile(node,vm)方法，实现data数据与视图的绑定，初始化视图
  - 在编译过程中，为每个与数据绑定相关的节点生成一个订阅者watcher，watcher将自己添加到相应属性的dep中

所以当数据变化时，相应的主体对象发布通知给订阅者，然后订阅者再去执行更新操作，使视图发生变化；当视图变化时给data的某个属性赋值，数据发生变化同样会发布通知给订阅者更新视图；

因为使用了Object.defineProperty,只能兼容到IE9，IE8及以下不兼容；

#### vue3.x 数据绑定原理：
```
new proxy = new Proxy({},handler)
var handler() {
    get(){},
    set() {},
    apply(){}, // 函数调用，apply,call操作时拦截
    has(){},    // has方法用来拦截HasProperty操作
    deleteProperty() {},
    defineProperty(){}, // 拦截defineProperty()方法
}
```
- Proxy()数据劫持可以实现数组的响应式变化，更简单；缺点是兼容性不够好，IE不兼容


[参考文章](https://juejin.im/entry/59116fa6a0bb9f0058aaaa4c)

### 2. Vue组件的数据通信方式

- [vue组件通信方式总结](./dataCommutate/dataCommutate.md)

### 3. Vuex的作用及应用场景

https://juejin.im/post/5a3e0fa05188252103347507

https://yq.aliyun.com/articles/613429

### 4. Vue源码看过吗？Vuex工作原理？

#### 看过一部分；
#### 4.1. 使用Vuex只需执行 Vue.use(Vuex)，并在Vue的配置中传入一个store对象的示例，store是如何实现注入的？
- VUe.use(Vuex)执行了install方法，该方法对Vue实例对象上的_init()进行封装和注入，将传入的store对象挂载到Vue上下文环境的$store中.因此vue中的任意组件都可以通过this.$store访问到；
#### 4.2.state内部支持模块配置和模块嵌套，如何实现的？
- 在store构造方法中有makeLocalContext方法，所有module都会有一个local context，根据配置时的path进行匹配。所以执行如dispatch('submitOrder', payload)这类action时，默认的拿到都是module的local state，如果要访问最外层或者是其他module的state，只能从rootState按照path路径逐步进行访问。
#### 4.3. 在执行dispatch触发action(commit同理)的时候，只需传入(type, payload)，action执行函数中第一个参数store从哪里获取的？

- store初始化时，所有配置的action和mutation以及getters均被封装过。在执行如dispatch('submitOrder', payload)的时候，actions中type为submitOrder的所有处理方法都是被封装后的，其第一个参数为当前的store对象，所以能够获取到 { dispatch, commit, state, rootState } 等数据。

#### 4.4.uex如何区分state是外部直接修改，还是通过mutation方法修改的？
- Vuex中修改state的唯一渠道就是执行 commit('xx', payload) 方法，其底层通过执行 this._withCommit(fn) 设置_committing标志变量为true，然后才能修改state，修改完毕还需要还原_committing变量。外部修改虽然能够直接修改state，但是并没有修改_committing标志位，所以只要watch一下state，state change时判断是否_committing值为true，即可判断修改的合法性。

#### 4.5. 调试时的”时空穿梭”功能是如何实现的？
- devtoolPlugin中提供了此功能。因为dev模式下所有的state change都会被记录下来，’时空穿梭’ 功能其实就是将当前的state替换为记录中某个时刻的state状态，利用 store.replaceState(targetState) 方法将执行this._vm.state = state 实现。
  
### 5. 虚拟Dom会提高性能，解释其工作原理

### 6. 请详细介绍package.json里面的配置

### 7. 为什么说vue是一套渐进式框架

### 8. vue-cli提供的几种脚手架模板有哪些，区别是什么？

### 9. 计算属性的缓存和方法调用的区别

### 10. axios,fetch与ajax有什么区别

### 11. vue中央事件总线的使用

### 12. 你做过vue项目有哪些，用到了哪些核心知识点

### 13. 实现MVVM的思路分析

### 14. 批量异步更新策略及nextTick原理

### 15. 说说vue开发命令npm run dev 输入后的执行过程

### 16. vue-cli中常用到的加载器有哪些

### 17. vue中如何使用keep-alive标签实现某个组件缓存功能

### 18. pc端页面刷新时如何实现vuex缓存

https://blog.csdn.net/guzhao593/article/details/81435342

https://juejin.im/post/5b62999fe51d4519610e336e

### 19. vue-router如何响应路由参数的变化

### 20. Vue组件中data为什么必须是函数

https://www.imqianduan.com/vue/192.html

