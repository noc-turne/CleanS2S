from typing import Dict, List, Optional, Union, Any
import json
import os
from openai import OpenAI
from s2s_server_pipeline import LanguageModelAPIHandler


class APIQueryHelper:

    def __init__(self, model_url, model_name):
        self.client = OpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=model_url)
        self.model_name = model_name

    def query(self, umsg, sysp, temperature=0.6, isjson=False):
        msg = [
            {
                "role": "system",
                "content": sysp
            },
            {
                "role": "user",
                "content": umsg
            },
        ]

        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=msg,
            max_tokens=4096,
            temperature=temperature,
            stream=False,
            frequency_penalty=0,
            presence_penalty=0,
            top_p=0.95,
            logprobs=False,
            response_format={"type": "json_object"} if isjson else None
        )
        t = response.choices[0].message.content

        if isjson:
            try:
                res = json.loads(t)
            except json.JSONDecodeError:
                res = t
        else:
            res = t

        return res


class Memory:
    # Memory saves the facts，elements and history messages in conversation，history_len is used to decide the length of history messages. When the length of history messages is longer than history_len, 
    # the former messages will be processed and saved by summary. After every turn of conversation, self.facts and self.elements will be updated.

    def __init__(self, history_len, model_url, model_name) -> None:
        self.history_len = history_len  # The length of history messages to save
        self.facts = []
        self.summary = []
        self.history_list = ['' for _ in range(self.history_len)]
        # Break down key facts into multiple elements
        self.elements = {'时间': '', '地点': '', '气候': '', '人物': '', '动作': '', '态度': ''}
        self.api_helper = APIQueryHelper(model_url, model_name)
        self.base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
        self.memory_base_path = os.path.join(self.base_path, 'memory')

    def add(self, new_msg): # add new_msg to self.facts and self.summary
        out_msg = self.history_list.pop(0)
        self.history_list.append(new_msg)
        self.fact_func(new_msg)
        if out_msg:
            self.summary_func(out_msg)

    def get(self):
        fact = json.dumps(self.facts[-self.history_len // 2:], ensure_ascii=False)
        summary = json.dumps(self.summary, ensure_ascii=False)
        hist = json.dumps(self.history_list, ensure_ascii=False)
        elements = json.dumps(self.elements, ensure_ascii=False)
        return json.dumps({"关键事实": fact, "超限对话总结": summary, "关键事实要素": elements, "历史对话": hist}, ensure_ascii=False)

    def fact_func(self, msg):
        with open(os.path.join(self.memory_base_path, 'fact.txt'), "r", encoding='utf-8') as f:
            fact_sys_prompt = f.read()
        old_fact = self.facts[:-self.history_len // 2]
        self.facts.append(self.api_helper.query("对话：" + msg + "旧的关键事实：" + str(old_fact), fact_sys_prompt))

    def summary_func(self, msg):
        with open(os.path.join(self.memory_base_path, 'summary.txt'), "r", encoding='utf-8') as f:
            summary_sys_prompt = f.read()
        old_summary = self.summary
        self.summary = self.api_helper.query("对话：" + msg + "旧的总结：" + old_summary, summary_sys_prompt)

    def update(self, update_elements_list, msg):  # update self.elements according to the result of self.panding()
        for update_element in update_elements_list:
            if self.elements[update_element] == '':  # not initialized yet
                with open(os.path.join(self.memory_base_path, "initialize.txt"), "r", encoding='utf-8') as f:
                    initialize_sys_prompt = f.read()
                umsg = f"对话：{msg}, 要素：{update_element}"
                initialization = self.api_helper.query(umsg, initialize_sys_prompt)
                self.elements[update_element] = initialization
            else:
                with open(os.path.join(self.memory_base_path, "update.txt"), "r", encoding='utf-8') as f:
                    update_sys_prompt = f.read()
                umsg = f"对话：{msg}, 要素：{update_element}, 已有要素内容：{self.elements[update_element]}"
                updated = self.api_helper.query(umsg, update_sys_prompt)
                self.elements[update_element] += updated

    def inside_conflict(
        self, msg, element
    ) -> bool:  #Judge whether there is a conflict between QA and element. Refer only to the content in self.elements
        with open(os.path.join(self.memory_base_path, "inside_conflict.txt"), "r", encoding='utf-8') as f:
            conflict_sys_prompt = f.read()
        umsg = f'对话：{msg}, 要素：{element}, 要素内容：{self.elements[element]}'
        reject_flag = self.api_helper.query(umsg, conflict_sys_prompt)
        if 'True' in reject_flag:
            return True
        else:
            return False

    def reject(self, msg):  # Refer only to the content in self.elements
        reject_list = []
        for element in self.elements.keys():
            if self.elements[element] != '':  # already initialized
                conflict_flag = self.inside_conflict(msg, element)
                if conflict_flag:
                    with open(os.path.join(self.memory_base_path, "reject.txt"), "r", encoding='utf-8') as f:
                        reject_sys_prompt = f.read()
                    umsg = f'对话：{msg}, 要素：{element}, 要素内容：{self.elements[element]}'
                    reject = self.api_helper.query(umsg, reject_sys_prompt)
                    reject_list.append({'要素': element, '原因': reject})
        if len(reject_list) > 0:
            return True, reject_list
        return False, ''

    def panding(self, msg):  # decide which elements to be considered
        with open(os.path.join(self.memory_base_path, "panding.txt"), "r", encoding='utf-8') as f:
            panding_sys_prompt = f.read()
        umsg = f"用户输入：{msg},关键事实种类列表：{self.elements.keys()}"
        elements_list = self.api_helper.query(umsg, panding_sys_prompt, isjson=True)
        if isinstance(elements_list, dict):
            first_key = list(elements_list.keys())[0]
            elements_list = elements_list[first_key]
        return elements_list

    def process(self, msg): # finally process msg, reject or update
        reject_flag, reject_result = self.reject(msg)
        if reject_flag:  #If there is a conflict
            return False, reject_result
        else:
            elements_list = self.panding(msg)
            self.update(elements_list, msg)
            return True, ''

    def clear(self):
        self.facts = []
        self.summary = []
        self.history_list = ['' for _ in range(self.history_len)]


class MemoryChatHelper:

    def __init__(self, model_url, model_name, character='anlingrong.txt', history_len=5) -> None:

        self.memory = Memory(history_len, model_url, model_name)
        self.character_base_path = os.path.join(self.memory.base_path, 'character')
        with open(os.path.join(self.memory.base_path, character), "r", encoding='utf-8') as f:
            self.c_sys_prompt = f.read()

    def generate_sys_prompt(self, msg):
        his_msg = json.dumps(self.memory.history_list, ensure_ascii=False)
        re_type = self.judge('历史对话：' + his_msg + 'user:' + msg)
        judge_type = ['敷衍', '延迟回复', '转移话题', '直白拒绝', '不回复', '正常回复']

        if re_type in ["1", "2", "3", "4", "5", "6"]:
            re_type = int(re_type)
        elif re_type in judge_type:
            re_type = judge_type.index(re_type) + 1
        else:
            re_type = 6

        # 为了测速先不实装这两个情况
        # if re_tyep == 2: # 延迟回复
        # time.sleep(1000)
        # elif re_tyep == 5: # 不回复
        #     res = ''
        # else: # 其他情况

        sys_p = self.c_sys_prompt
        sys_p += self.memory.get()
        # flag means whether the msg is rejected. True means not rejected
        flag, reject_result = self.memory.process(msg)
        sys_p += reject_result

        if re_type in [1, 3, 4]:
            sys_p += f'# 指导思想：此次回复的指导思想为:{judge_type[re_type-1]}'
        else:
            sys_p += f'# 指导思想：此次回复的指导思想为:正常回复'

        return sys_p

    def judge(self, msg):
        with open(os.path.join(self.memory.base_path, "./nci.txt"), "r", encoding='utf-8') as f:
            sys_p = f.read()
        res = self.memory.api_helper.query(msg, sys_p)
        return res


class LanguageModelAPIHandlerWithMemory(LanguageModelAPIHandler):

    def __init__(self, *args, character='anlingrong.txt', history_len=5, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory_chat_helper = MemoryChatHelper(self.model_url, self.model_name, character, history_len)

    def process(self, inputs: Dict[str, Union[str, int, bool]]) -> Dict[str, Union[str, int, bool]]:
        """
        Process the input acquired from queue_in (from ASR/STT) and generate the output of the language model API with
        the stream paradigm, i.e., yield the generated subtext in real-time.
        Arguments:
            - inputs (Dict[str, Union[str, int, bool]): The input data acquired from queue_in. The data contains the \
                str format audio transcripted data, user id(uid), bool flag to indicate whether the audio or the text \
                input from user and integer user input count.
        Returns (Yield):
            - output (Dict[str, Union[str, int, bool]]): The output data containing the transcripted question text, \
                the generated answer text, end flag for the current LLM API generation, uid and the user input count.
        """

        generator = super().process(inputs)
        total_answer = ""
        for ele in generator:
            if isinstance(ele['answer_text'], str):
                total_answer += ele['answer_text']
            yield ele
        self.memory_chat_helper.memory.add(json.dumps({"user": inputs['data'], "AI": total_answer}, ensure_ascii=False))

    def _before_process(self, prompt: str, count: int) -> List[Dict[str, str]]:
        """
        Preparation chat messages before the generation process.
        Arguments:
            - prompt (str): The input prompt in current step.
            - count (int): The user input count.
        Returns:
            - messages (List[Dict[str, str]): The chat messages.
        """

        sys_p = self.memory_chat_helper.generate_sys_prompt(prompt)
        self.chat.init_chat({"role": 'system', "content": sys_p})
        self.chat.append({"role": self.user_role, "content": prompt})
        return self.chat.to_list()

    def clear_current_state(self):
        super().clear_current_state()
        self.memory_chat_helper.memory.clear()