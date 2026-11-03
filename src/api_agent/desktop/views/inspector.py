"""Постраничный JSON inspector; только сохранённые sections выбранного exchange."""
import json
from rich.text import Text
from textual.widgets import Tree
from api_agent.desktop.presentation.presentation import duration, error_text
from api_agent.goals.checks.compiler import PRIVATE_NAMES


def child(path,key):return path+'/'+str(key).replace('~','~0').replace('/','~1')


class JsonInspector(Tree):
    BINDINGS=[('space','select_field','Выбрать поле')]
    def __init__(self,**kwargs):
        super().__init__('Выберите HTTP-обмен',**kwargs)
        self.loader=None;self.field_picker=None;self.chosen=set()

    def reset_exchange(self):
        self.clear();self.loader=None;self.field_picker=None;self.chosen.clear()
        self.root.set_label(Text('Выберите HTTP-обмен'))

    def show_exchange(self,loader,*,field_picker=None,metadata=None):
        self.clear();self.loader=loader;self.field_picker=field_picker;self.chosen.clear()
        self.root.set_label(Text('Фактические HTTP-данные'))
        for section,title in [('request','Запрос'),('response','Ответ')]:
            node=self.root.add(Text(title),data={'section':section,'path':'','loaded':False},allow_expand=True)
            self.load(node)
        if metadata:
            seconds=metadata.get('wall_seconds')
            self.root.add_leaf(Text('Длительность: '+(duration(seconds) if seconds is not None else 'не сохранена')))
            self.root.add_leaf(Text('Исход: '+{'received':'ответ получен','timeout':'истекло время','disconnect':'соединение разорвано'}.get(metadata.get('outcome'),str(metadata.get('outcome') or 'не сохранён'))+
                ' · Транспорт: '+str(metadata.get('transport_error') or 'нет сохранённой ошибки')))
            if metadata.get('effect_status')=='uncertain':self.root.add_leaf(Text('Эффект записи: НЕОПРЕДЕЛЁН; слепой повтор запрещён'))
            elif metadata.get('effect_status'):self.root.add_leaf(Text('Эффект записи: '+str(metadata['effect_status'])))
            self.root.add_leaf(Text('Усечение: '+('да; полнота не подтверждена' if metadata.get('truncated') else 'нет')))
            if metadata.get('request_parameters'):self.root.add_leaf(Text('Параметры запроса: '+json.dumps(metadata['request_parameters'],ensure_ascii=False)))
        self.root.expand()

    def load(self,node):
        data=node.data
        if not self.loader or not isinstance(data,dict) or data.get('loaded'):return
        data['loaded']=True
        try:page=self.loader(data['section'],data['path'],data.get('offset',0))
        except ValueError as error:node.add_leaf(Text('Недоступно: '+error_text(error)));return
        if page.get('source_truncated'):node.add_leaf(Text('Ответ усечён: полнота не подтверждена'))
        value=page['value']
        if not data['path'] and (not isinstance(value,dict) or 'headers' not in value):
            node.add_leaf(Text('Заголовки: не сохранены / ответ не получен'))
        entries=list(value.items()) if isinstance(value,dict) else list(enumerate(value,start=page['offset'])) if isinstance(value,list) else [(None,value)]
        for key,item in entries:
            path=child(data['path'],key) if key is not None else data['path']
            container=isinstance(item,(dict,list))
            label=(str(key)+': ' if key is not None else '')+('{…}' if isinstance(item,dict) else '[…]' if isinstance(item,list) else json.dumps(item,ensure_ascii=False))
            selectable=data['section']=='response' and path.startswith('/json/') and not container and str(key).lower() not in PRIVATE_NAMES and item not in ('[REDACTED]','[NOT_CLASSIFIED]')
            if selectable:label+='  '+('☑' if path[len('/json'):] in self.chosen else '☐')
            info={'section':data['section'],'path':path,'loaded':not container,'scalar':selectable,'label':label,'value':item}
            if container:node.add(Text(label,style='cyan'),data=info,allow_expand=True)
            else:node.add_leaf(Text(label,style='green' if isinstance(item,str) else 'yellow' if isinstance(item,(int,float)) else 'magenta'),data=info)
        if page['next_offset'] is not None:
            node.add(Text('Показать ещё…'),data={'section':data['section'],'path':data['path'],
                'offset':page['next_offset'],'loaded':False},allow_expand=True)
        if not page['page_complete']:node.add_leaf(Text('Часть вложенных данных скрыта; раскройте нужное поле'))

    def on_tree_node_expanded(self,event):self.load(event.node)

    def on_tree_node_selected(self,event):
        if isinstance(event.node.data,dict) and event.node.data.get('scalar'):
            event.stop();self.action_select_field()

    def action_select_field(self):
        node=self.cursor_node
        if node is None or not isinstance(node.data,dict) or not node.data.get('scalar'):return
        path=node.data['path'][len('/json'):]
        selected=set(self.chosen)
        if path in selected:selected.remove(path)
        else:selected.add(path)
        if self.field_picker:
            try:self.field_picker(sorted(selected))
            except ValueError as error:self.app.notify(error_text(error),severity='warning');return
        self.chosen=selected
        node.set_label(Text(node.data['label'][:-1]+('☑' if path in selected else '☐')))
