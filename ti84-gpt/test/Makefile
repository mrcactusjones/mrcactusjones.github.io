# Desktop tests for the firmware logic (no hardware needed).
#   make -C test
CXX ?= g++
CXXFLAGS ?= -std=c++17 -O1 -g -Wall -Wextra -pthread
FW := ../TI84GPT
SOURCES := test_main.cpp sim.cpp mini_basic.cpp \
           $(FW)/ti_link.cpp $(FW)/ti_vars.cpp $(FW)/ti_tokens.cpp \
           $(FW)/text_layout.cpp $(FW)/calc_app.cpp $(FW)/json_mini.cpp $(FW)/chat.cpp

.PHONY: test clean
test: run_tests
	./run_tests ..

run_tests: $(SOURCES) $(wildcard *.h) $(wildcard $(FW)/*.h)
	$(CXX) $(CXXFLAGS) -o $@ $(SOURCES)

clean:
	rm -f run_tests
