from typing import Any, List, Dict, Union, Tuple, Optional
import json, re
from textwrap import dedent
from typing import List, Dict
import numpy as np
from difflib import SequenceMatcher
from sklearn.cluster import AgglomerativeClustering
from collections import Counter


from opto.trace.propagators import GraphPropagator
from opto.optimizers.optoprime import OptoPrime


class OptoPrimeMulti(OptoPrime):
    def __init__(
        self,
        *args,
        num_responses: int = 3,
        temperature_min_max: Optional[List[float]] = None,
        selector: Optional[callable] = None,
        generation_technique: str = "temperature_variation",
        selection_technique: str = "best_of_n",
        experts_list: Optional[List[str]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.temperature_min_max = temperature_min_max if temperature_min_max is not None else [0.0, 1.0]
        self.candidates = []  # Store all candidate solutions
        self.selected_candidate = None  # Store the selected candidate solution
        self.num_responses = num_responses
        self.selector = selector
        self.generation_technique = generation_technique
        self.selection_technique = selection_technique
        self.experts_list = experts_list

    def call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        verbose: Union[bool, str] = False,
        max_tokens: int = 4096,
        num_responses: int = 1,
        temperature: float = 0.0,
    ) -> List[str]:
        """Call the LLM with a prompt and return multiple responses."""
        # if verbose not in (False, "output"):
        #     print("Prompt\n", system_prompt + user_prompt)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            if hasattr(self.llm, "create"):
                # Standard OpenAI/LangChain style
                response = self.llm.create(
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens,
                    n=num_responses,
                    temperature=temperature,
                )
            else:
                # Fallback for LiteLLM (callable) or other interfaces
                # e.g., LiteLLM(messages, max_tokens=…, n=…, temperature=…)
                response = self.llm(
                    messages,
                    max_tokens=max_tokens,
                    n=num_responses,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
        except Exception as e:
            if verbose:
                print(f"ERROR {e}")
            return []  # or re-raise if you prefer

        responses = [choice.message.content for choice in response.choices]
        # if verbose:
        #     print("LLM responses:\n", responses)

        return responses

    def generate_candidates(
        self,
        summary,
        system_prompt: str,
        user_prompt: str,
        verbose: Union[bool, str] = False,
        mask=None,
        max_tokens: int = None,
        num_responses: int = 3,
        generation_technique: str = "temperature_variation",
        temperature_min_max: Optional[List[float]] = None,
        experts_list: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Generate multiple candidates using various techniques.
        Args:
            summary: The summarized problem instance.
            system_prompt (str): The system-level prompt.
            user_prompt (str): The user-level prompt.
            verbose (bool): Whether to print debug information.
            mask: Mask for the problem instance.
            max_tokens (int, optional): Maximum token limit for the LLM responses.
            num_responses (int): Number of responses to request.
            generation_technique (str): Technique to use for generation:
                - "temperature_variation": Use varying temperatures
                - "self_refinement": Each solution refines the previous one
                - "iterative_alternatives": Generate diverse alternatives
                - "multi_experts": Use different expert personas
            temperature_min_max (List[float], optional): [min, max] temperature range.
            experts_list (List[str], optional): List of expert personas to use for multi_experts technique.
        Returns:
            List[str]: List of LLM responses as strings.
        """
        import re  # Add explicit import for regex

        num_responses = num_responses if num_responses is not None else self.num_responses
        max_tokens = max_tokens or self.max_tokens
        temperature_min_max = temperature_min_max if temperature_min_max is not None else self.temperature_min_max
        candidates = []
        
        # Ensure temperature_min_max has at least 2 elements
        if not isinstance(temperature_min_max, list) or len(temperature_min_max) < 2:
            temp_min, temp_max = 0.0, 1.0  # Default values
        else:
            temp_min, temp_max = temperature_min_max[0], temperature_min_max[1]

        generation_technique = generation_technique.lower()

        if generation_technique == "self_refinement":
            # Generate solutions by refining previous ones
            for i in range(num_responses):
                if not candidates:
                    meta_prompt = system_prompt
                else:
                    meta_prompt = f"{system_prompt}\nRefine the previous solution to given problem in order to answer with a much better answer & suggestion to the problem (use the same JSON format / suggest only trainable codes/variables to modify, never inputs), PREVIOUS SOLUTION:<<<\n{candidates[-1]}\n>>>"
                    
                response = self.call_llm(
                    system_prompt=meta_prompt,
                    user_prompt=user_prompt,
                    verbose=verbose,
                    max_tokens=max_tokens,
                    num_responses=1,
                    temperature=0.0,
                )
                
                if response and len(response) > 0:
                    candidates.append(response[0])
        
        elif generation_technique == "iterative_alternatives":
            # Generate alternatives informed by previous solutions
            for i in range(num_responses):
                meta_prompt = system_prompt
                if i > 0 and candidates:
                    # Generate a new alternative based on all previous
                    previous_solutions = "\n".join(
                        f"CANDIDATE {idx + 1}: <<<\n{cand}\n>>>"
                        for idx, cand in enumerate(candidates)
                    )
                    meta_prompt = f"{system_prompt}\nGiven the following candidate solutions, propose a new alternative optimal solution to user's prompt using their same JSON format (suggest only trainable codes/variables to modify, never inputs):\n{previous_solutions}\n"
                
                response = self.call_llm(
                    system_prompt=meta_prompt,
                    user_prompt=user_prompt,
                    verbose=verbose,
                    max_tokens=max_tokens,
                    num_responses=1,
                    temperature=0.0,
                )
                
                if response and len(response) > 0:
                    candidates.append(response[0])

        elif generation_technique == "multi_experts":
            # 1. Determine expert list (either passed in or generated)
            experts = []
            if isinstance(experts_list, list) and all(isinstance(e, str) for e in experts_list):
                while len(experts) < num_responses:
                    experts.append(experts_list[len(experts) % len(experts_list)])

            else:
                # ask LLM to output a JSON array of expert persona strings
                expert_json = self.call_llm(
                    system_prompt="Generate a list of complementaty experts to optimize a problem as a JSON string array (example: [\"AI Engineer\", \"Compiler Specialist\", ...]).",
                    user_prompt=(
                        f"NUMBER OF EXPERTS TO GENERATE: {num_responses}\n"
                        f"PROBLEM SUBMITED TO EXPERTS:\n<<<\n{system_prompt}\n>>>\n"
                        f"JSON ARRAY LIST OF EXPERTS:"
                    ),
                    num_responses=1,
                    temperature=0.0,
                    verbose=verbose,
                )
                # Handle case where no response is returned
                if not expert_json or len(expert_json) == 0:
                    if verbose: print("Failed to generate expert list, using default experts")
                else:
                    try:
                        experts = json.loads(expert_json[0])
                    except json.JSONDecodeError:
                        print(f"Failed to parse expert JSON: {expert_json}")
                        experts = []
                    if not isinstance(experts, list):
                        if isinstance(experts, dict) and len(experts) == 1 and isinstance(next(iter(experts.values())), list):
                            experts = next(iter(experts.values()))
                        else:
                            if verbose: print(f"Expected JSON array for experts, got {experts} type {type(experts).__name__} => using default experts")
                            experts = []

                # if experts is empty or does not contain the expected number of experts, use default
                if not experts or len(experts) <= num_responses:
                    default_experts = ["Algorithm Expert", "Performance Optimizer", "Out of the box problem solver", "AI Engineer", "Compiler Specialist"]
                    while len(experts) < num_responses:
                        experts.append(default_experts[len(experts) % len(default_experts)])
            print(f"Generated experts: {experts}")

            # 2. For each expert, prepare a system prompt + user prompt
            calls = []
            #output_format = "JSON format {""reasoning"": <Your reasoning>,""answer"": <Your answer>, ""suggestion"": {<variable_1>: <suggested_value_1>,<variable_2>: <suggested_value_2>,...}"
            for expert in experts[:num_responses]:
                meta_prompt = f"You are a `{expert}`\nProvide your most optimized solution for the problem below.\n{self.output_format_prompt}"
                response = self.call_llm(
                    system_prompt=meta_prompt,
                    user_prompt=f"PROBLEM:\n\n{user_prompt}",
                    verbose=verbose,
                    max_tokens=max_tokens,
                    num_responses=1,
                    temperature=0.0,
                )
                
                if response and len(response) > 0:
                    text = response[0]
                    sol = text.strip().removeprefix('<<<').removesuffix('>>>').strip()
                    candidates.append(sol)
                else:
                    generation_technique = "temperature_variation"
                    candidates = []
                    print(f"Error in multi_experts mode: {str(e)} – falling back to temperature variation")

        # Default to temperature variation
        if not candidates or generation_technique == "temperature_variation":
            if generation_technique != "temperature_variation":
                print(f"Unknown generation technique: {generation_technique}, defaulting to temperature_variation")
            # Use progressive temperature variation to generate diverse candidates
            temperatures = [temp_max - i * (temp_max - temp_min) / max(1, num_responses - 1) for i in range(num_responses)]

            if verbose:
                print(f"Temperatures for responses: {temperatures}")

            for temp in temperatures:
                try:
                    response = self.call_llm(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        verbose=verbose,
                        max_tokens=max_tokens,
                        num_responses=1,
                        temperature=temp,
                    )
                    
                    if response and len(response) > 0:
                        candidates.append(response[0])
                    else:
                        if verbose:
                            print(f"Empty response at temperature {temp}")
                            
                except Exception as e:
                    if verbose:
                        print(f"Error generating candidate at temperature {temp}: {str(e)}")
        
        if not candidates and verbose:
            print("Warning: Failed to generate any candidates")
            
        if self.log is not None:
            self.log.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "response": candidates, "generation_technique": generation_technique})
            # only build a problem instance if we actually have one
            pi = self.problem_instance(summary) if summary is not None else {}
            self.summary_log.append({"problem_instance": pi, "summary": summary})
        return candidates

    def select_candidate(self, candidates: List, selection_technique="moa", problem_summary="") -> Dict:
        """
        Select the best response based on the candidates using various techniques.
        
        Args:
            candidates (List): List of candidate responses from generate_candidates.
            selection_technique (str): Technique to select the best response:
                - "moa" or "mixture_of_agents": Use LLM to mix the best elements of each response
                - "majority": Use LLM to choose the most frequent candidate
                - "lastofn" or "last_of_n" (choose also if selection technique is unknown): Simply return the last candidate
                
        Returns:
            Dict: The selected candidate or an empty dictionary if no candidates exist.
        """
        if not candidates:
            return {}
        elif len(candidates) <= 1:
            return candidates[0] if candidates else {}
        
        # Normalize selection technique name for case-insensitive comparison
        selection_technique = selection_technique.lower()
            
        # Extract text from candidates for analysis
        candidate_texts = []
        for candidate in candidates:
            if isinstance(candidate, dict):
                # For _step, candidates are dicts with various fields
                text = candidate.get("text", "")
                if not text and "suggestion" in candidate:
                    text = str(candidate["suggestion"])
            else:
                # In case we're passed raw strings
                text = str(candidate)
            candidate_texts.append(text)
        
        # Handle different selection techniques
        if selection_technique in ["moa", "mixture_of_agents"]:
            return self._select_moa(candidates, candidate_texts, problem_summary)
        elif selection_technique in ["bestofn", "best_of_n"]:
            return self._select_bestofn(candidates, candidate_texts, problem_summary)
        elif selection_technique in ["majority"]:
            return self._select_majority(candidates, candidate_texts, problem_summary)
        else:  # default to lastofn/last_of_n
            return candidates[-1]
            
    def _select_moa(self, candidates, candidate_texts, summary=None):
        """Mixture of Agents selection - combines best elements from all candidates"""
        # Construct the prompt for mixture of agents
        meta_prompt = (
            "You are an expert at synthesizing multiple solutions into a single optimal solution."
            "Given the following responses to a problem, provide an optimal response "
            "that mixes the best elements of each (suggest only trainable codes/variables to modify, never inputs)"
            f"{self.output_format_prompt}"
        )
        
        user_prompt = f"Problem:\n{summary}\n\n" if summary else ""
        # Add all candidate responses
        for i, text in enumerate(candidate_texts):
            user_prompt += f"Response {i + 1}:\n{text}\n\n"
            
        # Call LLM to synthesize a response
        system_prompt = meta_prompt
        response = self.call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_responses=1,
            temperature=0.0
        )
        
        return response[0] if (response and response[0]) else candidates[-1]
        
    def _select_bestofn(self, candidates, candidate_texts, summary=None):
        """Best of N selection - chooses the most promising candidate"""
        user_prompt = f"Problem:\n{summary}\n\n" if summary else ""
            
        # Add all candidate responses
        for i, text in enumerate(candidate_texts):
            user_prompt += f"Candidate {i + 1}:\n{text}\n\n"
            
        meta_prompt = (
            "You are an expert at evaluating solutions and selecting the most promising one."
            f"Given the following candidate solutions to a problem"
            "First, reason by analyzing each candidate's answer/suggestion strengths and weaknesses, then identify the reply with the most promising candidate. "
            f"{self.output_format_prompt}"
        )
        
        # Call LLM to select the best candidate
        response = self.call_llm(
            system_prompt=meta_prompt,
            user_prompt=user_prompt,
            num_responses=1,
            temperature=0.0
        )
        
        return response[0] if (response and response[0]) else candidates[-1]
        
    def _select_majority(self, candidates, candidate_texts, summary=None):
        """Majority selection - finds the consensus solution among candidates"""
        if len(candidate_texts) <= 1:
            return candidates[0] if candidates else {}
        
        # Check if we can use clustering approach
        try:
            import numpy as np
            from difflib import SequenceMatcher
            from sklearn.cluster import AgglomerativeClustering
            from collections import Counter
            
            # Build distance matrix based on text similarity
            n = len(candidate_texts)
            D = np.zeros((n, n))
            for i in range(n):
                for j in range(i + 1, n):
                    sim = SequenceMatcher(None, candidate_texts[i], candidate_texts[j]).ratio()
                    D[i, j] = D[j, i] = 1 - sim  # Convert similarity to distance
            
            # Cluster the responses using hierarchical clustering
            try:
                clu = AgglomerativeClustering( n_clusters=None, affinity="precomputed", linkage="complete", distance_threshold=0.2).fit(D) # old sklearn version
            except TypeError:
                clu = AgglomerativeClustering( n_clusters=None, metric="precomputed", linkage="complete", distance_threshold=0.2).fit(D) # new sklearn version >= 1.4

            # Find the largest cluster
            labels = clu.labels_
            if len(set(labels)) == 1:  # All in one cluster
                return candidates[-1]
                
            # Get the most common label (largest cluster)
            top_label = Counter(labels).most_common(1)[0][0]
            
            # Find indices of candidates in the largest cluster
            cluster_indices = [i for i, lab in enumerate(labels) if lab == top_label]
            
            # Find the medoid of the cluster (most central member)
            sub_distances = D[np.ix_(cluster_indices, cluster_indices)]
            medoid_idx_in_cluster = int(np.argmin(sub_distances.sum(axis=1)))
            medoid_idx = cluster_indices[medoid_idx_in_cluster]
            
            return candidates[medoid_idx]
            
        except (ImportError, Exception) as e:
            print(f"Error in majority selection: {str(e)} – falling back to last candidate")
            # Fallback to last candidate
            return candidates[-1]

    def _step(
        self,
        verbose=False,
        mask=None,
        num_responses: Optional[int] = None,
        temperature_min_max: Optional[List[float]] = None,
        selector: callable = None,
        generation_technique: str = None,
        selection_technique: str = None,
        experts_list: Optional[List[str]] = None,
        *args,
        **kwargs,
    ) -> Dict:
        """
        Perform a single optimization step, storing responses in self.responses and allowing selection.
        Args:
            verbose (bool): Whether to print debug information.
            mask (list, optional): Mask for the problem instance.
            num_responses (int): Number of responses to request from the LLM.
            temperature (float): Sampling temperature for the LLM.
            selector (callable, optional): Function to select the best response.
        Returns:
            Dict: The update dictionary based on the selected response.
        """
        num_responses = num_responses or self.num_responses
        temperature_min_max = temperature_min_max or self.temperature_min_max
        selector = selector or self.selector
        generation_technique = generation_technique or self.generation_technique
        selection_technique = selection_technique or self.selection_technique
        experts_list = experts_list or self.experts_list

        assert isinstance(self.propagator, GraphPropagator)
        summary = self.summarize()
        system_prompt, user_prompt = self.construct_prompt(summary, mask=mask)

        system_prompt = self.replace_symbols(system_prompt, self.prompt_symbols)
        user_prompt = self.replace_symbols(user_prompt, self.prompt_symbols)

        # Generate candidates
        self.candidates = self.generate_candidates(
            summary,
            system_prompt,
            user_prompt,
            verbose=verbose,
            mask=mask,
            num_responses=num_responses,
            temperature_min_max=temperature_min_max,
            generation_technique=generation_technique,
            experts_list=experts_list,
        )
        
        if verbose:
            print(f"OptoPrimeMulti > Generated candidates (self.candidates): {self.candidates}")

        if "TERMINATE" in self.candidates: return {}

        # Select the response using the selector or the default select_candidate method
        if selector and callable(selector):  # Ensure the selector is callable
            self.selected_candidate = selector(self.candidates)
        else:
            self.selected_candidate = self.select_candidate(candidates=self.candidates, selection_technique=selection_technique, problem_summary=system_prompt)
        
        if verbose: print(f"OptoPrimeMulti > Selected candidate (self.selected_candidate): {self.selected_candidate}")

        suggestion = self.extract_llm_suggestion(self.selected_candidate)
        if not suggestion:
            # Last-ditch: maybe caller already gave us the mapping
            if isinstance(self.selected_candidate, dict):
                if verbose: print("OptoPrimeMulti > No suggestion found, but candidate is a dict. Using it as suggestion.")
                suggestion = self.selected_candidate
        
        if verbose: print(f"OptoPrimeMulti > Extracted suggestion: {suggestion}")
        update_dict = self.construct_update_dict(suggestion)
        if verbose: print(f"OptoPrimeMulti > Constructed update_dict: {update_dict}")

        return update_dict
