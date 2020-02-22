### 一. 父子之间的通信：

1. props/$emit: props是单项数据流，数据由父组件 -> 子组件，子组件只能读取，不能直接修改 props 中的数据，否则会发出警告，修改无效；$emit允许子组件向父组件传递事件，从而将数据作为参数回传给父组件，父组件通过监听事件收集数据。

2. $parent/$children

3. provide/inject

4. ref

5. $attrs/$listeners


### 二. 兄弟组件的通信

1. EventBus 事件总线

2. Vuex状态管理


### 三. 跨级通信

1. EventBus

2. Vuex

3. provide/inject

4. $attrs/$listeners
