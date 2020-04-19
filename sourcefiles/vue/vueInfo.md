### 1. v-if和v-for哪个优先级高？如果两个同时出现，怎么优化得到更好的性能？

#### 源码来源：compiler/codegen/index.js
```
// 两者同级时，v-if是在循环里面判断的，v-for优先级高于v-if
<p v-for="item in children" v-if="isChildren" :key="item.id"></p>   
// 两者不同级
<template v-if="isChildren">
    <p v-for="item in children" :key="item.id"></p>
</template>
```
#### 结论：
- v-for优先于v-if
- 两者同时出现时，每次渲染都会先执行循环再判断，浪费了性能
- 在外层嵌套template进行v-if判断，在内部执行循环，可以提高性能

### 2.vue组件data为什么是个函数？而vue根组件示例没有此限制？
#### 源码来源：src/core/instance/state.js  -initData()

#### 结论：
- vue组件可能存在多个实例，如果使用对象形式定义data,由于对象是引用类型，导致多个组件实例共用一个data对象，组件中的data受到污染，显然这是不合理的；所以采用函数形式定义，每次返回全新的data对象，多个组件实例之间的data就不会相互影响。
- vue根组件data不限制，因为vue的根实例只有一个，不用担心根组件中的data被篡改的问题

### 3. 你知道vue中key的作用和工作原理吗？说说对它的理解。

#### 源码来源：src/core/vdom/patch.js -updateChildren()

- key的作用主要是为了高效地更新虚拟dom，其原理是vue在patch过程中通过key可以精准判断两个节点是否是同一个，从而避免频繁更新不同元素，使得整个patch过程更加高效，减少dom的操作量，提高性能
- 另外，不设置key可能在列表更新时引发一些隐蔽的bug
- vue中在使用相同标签名元素的过渡切换时，也会用到key目的是为了让vue区分它们，否则vue只会替换其内部属性而不会触发过渡效果。

#### 4. 你怎么理解vue中的diff算法？

#### 源码分析1：必要性，lifecycle.js -mountComponent()
#### 源码分析2： 执行方式， patch.js -patchVnode()
#### 源码分析3：高效性：patch.js  - updateChildren()

#### 结论：

- diff算法：是虚拟DOM技术的必然产物，通过新旧虚拟DOM作对比，将变化的地方更新在真是DOM上；另外，需要diff高效的执行对比过程，从而降低时间复杂度为o(n)
- vue2.x中为了降低Watcher粒度，每个组件只有一个Watcher与之对应，只有引入diff才能精确找到发生变化的地方
- vue中diff执行的时刻是组件实例执行其更新函数时，它会比对上一次渲染结果oldVnode和新的渲染结果newVnode,此过程成为patch
- diff过程整体遵循深度优先，同层比较的策略；两个节点之前比较会根据他们是否拥有子节点或者文本节点做不同操作；比较两组子节点是算法的重点，首先假设头尾节点可能相同做4次比对尝试，如果没有找到相同节点，再按照通用方式遍历查找，查找结束再按情况处理剩下的节点；借助key通常可以非常精确找到相同节点，因此整个patch过程非常高效；

### 5. 谈一谈vue组件化的理解？
#### 组件化定义，优点，使用场景和注意事项方面展开陈述；


### 6. 谈一谈你对vue设计原则的理解？

#### 渐进式javascript框架
- vue设计为可以自底向上逐层应用；vue核心库只关注视图层，不仅易于上手，还便于与第三方库结合；另一方面，当与现代化的工具链以及各种类库结合使用时，vue也能够为复杂的单页面应用提供驱动；

#### 易用，灵活和高效
- 易用性：vue提供数据响应式，声明式模板语法和基于配置的组件系统等核心特性。只需要关注应用的核心业务即可，上手轻松；
- 灵活性：渐进式框架的最大优点就是灵活性，如果应用足够小，我们仅需要vue核心特性即可完成功能，随着应用规模不断扩大，我们才逐渐引入路由，状态管理，vue-cli等工具和库，不管是应用的体积还是学习难度都是一个逐渐增加的平和曲线
- 高效性： 虚拟DOM和diff算法使我们的应用拥有最佳的性能表现。最求高效的过程还在继续，vue3中引入了proxy对数据响应式改进及编译器对于静态内容编译的改进都会让vue更加高效；

### 7. 谈谈你对MVC,MVP和MVVM的理解？
- 这三者都是框架模式，设计目的都是为了解决Model和View的耦合问题
- MVC模式出现较早主要应用在后端，如SpringMVC,ASP.NETMVC等。在前端领域的早期也有应用，如Backbone.js。它的优点是分层清晰，缺点是数据流混乱，灵活性带来了维护问题
- MVP: 添加了Presenter用于处理模型和逻辑，避免MV耦合；
- MVVM(model-view-viewmodel)模式在前端领域有广泛应用，它不仅解决MV耦合问题，还同时解决了维护两者映射关系的大量繁杂代码和DOM操作代码，在提高开发效率，可读性的同时还保持了优越的性能表现

### 8.你了解哪些Vue性能优化
- 路由懒加载
  ```
    const router = new vueRouter({
        routes:[
            {
                path:'/foo',
                component: ()=>import('./foo.vue')
            }
        ]
    })
  ```
- keep-alive缓存页面

- 使用v-show复用DOM
  
- v-for遍历避免同时使用v-if
  
- 长列表性能优化
   - 如果列表是纯粹的数据展示，不会有任何改变，就不需要做响应化
    ```
    export default {
        data(){
            return {
                users: []
            }
        }
        async created() {
            const users = await axios.get("/api/users")
            this.users = object.freeze(users)
        }
    }
    ```
   - 如果是大数据长列表，可采用虚拟滚动，只渲染少部分区域的内容
   ```
   <recycle-scroller :items="items" :item-size="24">
    <template v-slot="{item}">
        <FetchItemView :item="item" @vote="voteItem(item)">
    </template>    
   </recycle-scroller>
   ```
   #### 参考vue-virtual-scroller,vue-virtual-scrool-list
 - 事件销毁：Vue组件销毁时，会自动解绑它的全部指令及事件监听器，但是仅限于组件本身。例如计时器，一般在beforeDestroy()钩子中销毁
 - 图片懒加载： 第三方库vue-lazyload
 - 第三放插件按需引入
 - 无状态组件标记为函数式组件
    ```
    <template functional>
        <div>无状态组件，只是展示</div> 
    </template>
    ```
- 子组件分割
    ```
    <template>
        <div>子组件分割</div> 
        <childcomp />
    </template>
    export default {
        components: {
            childcomp: {
                methods: {
                    heavy() {}
                },
                render(h) {
                    return h('div',this.heavy())
                }
            }
        }
    }
    ```
- 变量本地化： computed属性缓存变量
- SSR: 首屏渲染，SEO优化
### 9. 你对vue3.0 新特性有没有了解

#### 更快：
- 基于Proxy的响应式系统
- 虚拟DOM重写
- 优化slots的生成
- 静态树提升
- 静态属性提升
#### 更小：通过摇树优化核心库体积
#### 更容易维护： TypeScript + Vue
#### 更友好：
- 跨平台：编译器核心和运行时核心与平台无关，使得vue更容易与任何平台一起使用
#### 更容易使用：
- 改进的ts支持
- 更好的调试支持
- 独立的响应式模块
- Composition API
